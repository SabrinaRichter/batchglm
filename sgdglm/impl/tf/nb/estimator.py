import abc

import xarray as xr
import numpy as np
import tensorflow as tf

try:
    import anndata
except ImportError:
    anndata = None

import impl.tf.train as train_utils
import impl.tf.ops as op_utils
from .external import AbstractEstimator, XArrayEstimatorStore, MonitoredTFEstimator, TFEstimatorGraph, InputData
from . import util as nb_utils

ESTIMATOR_PARAMS = AbstractEstimator.param_shapes().copy()
ESTIMATOR_PARAMS.update({
    "mu_raw": ("features",),
    "r_raw": ("features",),
    "sigma2_raw": ("features",),
})


class EstimatorGraph(TFEstimatorGraph):
    X: tf.Tensor

    mu: tf.Tensor
    r: tf.Tensor
    sigma2: tf.Tensor

    def __init__(self, X, num_observations, num_features, graph=None, optimizable=True):
        super().__init__(graph)

        # initial graph elements
        with self.graph.as_default():
            # # does not work due to broken tf.Variable initialization.
            # # Integrate data directly into the graph, until this issue is fixed.
            # X_var = tf_ops.caching_placeholder(
            #     tf.float32,
            #     shape=(num_observations, num_features), name="X")
            # X = X_var.initialized_value()

            learning_rate = tf.placeholder(tf.float32, shape=(), name="learning_rate")

            distribution, mu_var, r_var = nb_utils.fit(X=X, optimizable=optimizable, name="fit_nb-dist")

            with tf.name_scope("probs"):
                probs = distribution.prob(X, name="probs")
                probs = tf.clip_by_value(
                    probs,
                    0.,
                    1.
                )

            with tf.name_scope("log_probs"):
                log_probs = distribution.log_prob(X, name="log_probs")
                log_probs = tf.clip_by_value(
                    log_probs,
                    np.log(np.nextafter(0, 1, dtype=log_probs.dtype.as_numpy_dtype)),
                    0.
                )

            log_likelihood = tf.reduce_sum(log_probs, name="log_likelihood")
            with tf.name_scope("loss"):
                loss = -tf.reduce_mean(log_probs)

            trainable_variables = [mu_var, r_var]
            # ### management
            with tf.name_scope("training"):
                trainers = train_utils.MultiTrainer(
                    loss=loss,
                    variables=trainable_variables,
                    learning_rate=learning_rate
                )
                gradient = trainers.gradient

                aggregated_gradient = tf.add_n(
                    [tf.reduce_sum(tf.abs(grad), axis=0) for (grad, var) in gradient])

            with tf.name_scope("hessian_diagonal"):
                hessians = tf.hessians(loss, [mu_var, r_var])
                hessian_diagonal = [tf.diag_part(h) for h in hessians]
                hessian_diagonal = tf.transpose(tf.concat(hessian_diagonal, axis=0))

            # set up class attributes
            self.X = X

            self.mu_raw = tf.squeeze(distribution.mean())
            self.r_raw = tf.squeeze(distribution.r)
            self.sigma2_raw = tf.squeeze(distribution.variance())

            self.mu = tf.tile(distribution.mean(), (num_observations, 1))
            self.r = tf.tile(distribution.r, (num_observations, 1))
            self.sigma2 = tf.tile(distribution.variance(), (num_observations, 1))

            self.distribution = distribution
            self.probs = probs
            self.log_probs = log_probs
            self.log_likelihood = log_likelihood
            self.loss = loss
            self.hessian_diagonal = hessian_diagonal

            self.trainers = trainers
            self.gradient = aggregated_gradient
            self.plain_gradient = gradient
            self.global_step = trainers.global_step
            self.train_op = trainers.train_op_GD
            self.init_op = tf.global_variables_initializer()


class Estimator(AbstractEstimator, MonitoredTFEstimator, metaclass=abc.ABCMeta):
    model: EstimatorGraph

    @classmethod
    def param_shapes(cls) -> dict:
        return ESTIMATOR_PARAMS

    def __init__(self, input_data: InputData, model=None, graph=None, fast=False):
        self._input_data = input_data

        if model is None:
            if graph is None:
                graph = tf.Graph()

            model = EstimatorGraph(input_data.X.values,
                                   input_data.num_observations, input_data.num_features,
                                   optimizable=not fast,
                                   graph=graph)

        MonitoredTFEstimator.__init__(self, model)

    def _scaffold(self):
        with self.model.graph.as_default():
            scaffold = tf.train.Scaffold(
                init_op=self.model.init_op,
            )
        return scaffold

    def train(self, *args, learning_rate=0.05, **kwargs):
        tf.logging.info("learning rate: %s" % learning_rate)
        super().train(feed_dict={"learning_rate:0": learning_rate})

    @property
    def input_data(self) -> InputData:
        return self._input_data

    @property
    def X(self):
        return self.input_data.X

    @property
    def mu_raw(self) -> xr.DataArray:
        return self.to_xarray("mu_raw")

    @property
    def r_raw(self) -> xr.DataArray:
        return self.to_xarray("r_raw")

    @property
    def sigma2_raw(self) -> xr.DataArray:
        return self.mu_raw + ((self.mu_raw * self.mu_raw) / self.r_raw)

    @property
    def mu(self):
        mu = self.mu_raw.expand_dims(dim="observations", axis=0)
        mu = mu.isel(observations=np.repeat(0, self.input_data.num_observations))
        return mu

    @property
    def r(self):
        r = self.mu_raw.expand_dims(dim="observations", axis=0)
        r = r.isel(observations=np.repeat(0, self.input_data.num_observations))
        return r

    # @property
    # def sigma2(self):
    #     sigma2 = self.mu_raw.expand_dims(dim="observations", axis=0)
    #     sigma2 = sigma2.isel(observations=np.repeat(0, self.input_data.num_samples))
    #     return sigma2

    @property
    def loss(self):
        return self.to_xarray("loss")

    @property
    def gradient(self):
        return self.to_xarray("gradient")

    @property
    def hessian_diagonal(self):
        return self.to_xarray("hessian_diagonal")

    def finalize(self):
        store = XArrayEstimatorStore(self)
        self.close_session()
        return store