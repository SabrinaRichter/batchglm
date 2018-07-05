import abc
from typing import Union, Dict, Tuple
import logging

import xarray as xr
import tensorflow as tf
import numpy as np

try:
    import anndata
except ImportError:
    anndata = None

import impl.tf.ops as op_utils
import impl.tf.train as train_utils
from .external import AbstractEstimator, XArrayEstimatorStore, InputData, MonitoredTFEstimator, TFEstimatorGraph
from .external import nb_utils, tf_linreg

ESTIMATOR_PARAMS = AbstractEstimator.param_shapes().copy()
ESTIMATOR_PARAMS.update({
    "batch_probs": ("batch_observations", "features"),
    "batch_log_probs": ("batch_observations", "features"),
    "batch_log_likelihood": (),
    "full_loss": (),
    "full_gradient": ("features",),
})

logger = logging.getLogger(__name__)


# session / device config
# CONFIG = tf.ConfigProto(allow_soft_placement=True, log_device_placement=True)

class BasicModel:

    def __init__(self, X, design, a, b):
        dist_estim = nb_utils.NegativeBinomial(mean=tf.exp(tf.gather(a, 0)),
                                               r=tf.exp(tf.gather(b, 0)),
                                               name="dist_estim")

        with tf.name_scope("mu"):
            log_mu = tf.matmul(design, a, name="log_mu_obs")
            log_mu = tf.clip_by_value(
                log_mu,
                np.log(np.nextafter(0, 1, dtype=log_mu.dtype.as_numpy_dtype)),
                np.log(log_mu.dtype.max)
            )
            mu = tf.exp(log_mu)

        with tf.name_scope("r"):
            log_r = tf.matmul(design, b, name="log_r_obs")
            log_r = tf.clip_by_value(
                log_r,
                np.log(np.nextafter(0, 1, dtype=log_r.dtype.as_numpy_dtype)),
                np.log(log_r.dtype.max)
            )
            r = tf.exp(log_r)

        dist_obs = nb_utils.NegativeBinomial(mean=mu, r=r, name="dist_obs")

        with tf.name_scope("probs"):
            probs = dist_obs.prob(X)
            probs = tf.clip_by_value(
                probs,
                0.,
                1.
            )

        with tf.name_scope("log_probs"):
            log_probs = dist_obs.log_prob(X)
            log_probs = tf.clip_by_value(
                log_probs,
                np.log(np.nextafter(0, 1, dtype=log_probs.dtype.as_numpy_dtype)),
                0.
            )

        self.X = X
        self.design = design

        self.dist_estim = dist_estim
        self.mu_estim = dist_estim.mean()
        self.r_estim = dist_estim.r
        self.sigma2_estim = dist_estim.variance()

        self.dist_obs = dist_obs
        self.mu = mu
        self.r = r
        self.sigma2 = dist_obs.variance()

        self.probs = probs
        self.log_probs = log_probs
        self.log_likelihood = tf.reduce_sum(self.log_probs, name="log_likelihood")

        with tf.name_scope("loss"):
            self.loss = -tf.reduce_mean(self.log_probs)


class ModelVars:
    a: tf.Tensor
    a_intercept: tf.Variable
    a_slope: tf.Variable
    b: tf.Tensor
    b_intercept: tf.Variable
    b_slope: tf.Variable

    def __init__(
            self,
            X,
            design,
            name="Linear_Batch_Model",
            init_a_intercept=None,
            init_a_slopes=None,
            init_b_intercept=None,
            init_b_slopes=None,
    ):
        with tf.name_scope(name):
            num_design_params = design.shape[-1]
            (batch_size, num_features) = X.shape

            assert X.shape == [batch_size, num_features]
            assert design.shape == [batch_size, num_design_params]

            with tf.name_scope("initialization"):
                # implicit broadcasting of X and initial_mixture_probs to
                #   shape (num_mixtures, num_observations, num_features)
                init_dist = nb_utils.fit(X, axis=-2)
                assert init_dist.r.shape == [1, num_features]

                if init_a_intercept is None:
                    init_a_intercept = tf.log(init_dist.mean())
                    init_a_intercept = tf.clip_by_value(
                        init_a_intercept,
                        clip_value_min=np.log(1),
                        clip_value_max=np.log(init_a_intercept.dtype.max) / 8
                    )
                else:
                    init_a_intercept = tf.convert_to_tensor(init_a_intercept, dtype=X.dtype)

                if init_b_intercept is None:
                    init_b_intercept = tf.log(init_dist.r)
                    init_b_intercept = tf.clip_by_value(
                        init_b_intercept,
                        clip_value_min=np.log(1),
                        clip_value_max=np.log(init_a_intercept.dtype.max) / 8
                    )
                else:
                    init_b_intercept = tf.convert_to_tensor(init_b_intercept, dtype=X.dtype)
                assert init_b_intercept.shape == [1, num_features] == init_b_intercept.shape

                if init_a_slopes is None:
                    init_a_slopes = tf.random_uniform(
                        tf.TensorShape([num_design_params - 1, num_features]),
                        minval=np.nextafter(0, 1, dtype=design.dtype.as_numpy_dtype),
                        maxval=np.sqrt(np.nextafter(0, 1, dtype=design.dtype.as_numpy_dtype)),
                        dtype=design.dtype
                    )
                else:
                    init_a_slopes = tf.convert_to_tensor(init_a_slopes, dtype=X.dtype)

                if init_b_slopes is None:
                    init_b_slopes = init_a_slopes
                else:
                    init_b_slopes = tf.convert_to_tensor(init_b_slopes, dtype=X.dtype)

            a, a_intercept, a_slope = tf_linreg.param_variable(init_a_intercept, init_a_slopes, name="a")
            b, b_intercept, b_slope = tf_linreg.param_variable(init_b_intercept, init_b_slopes, name="b")
            assert a.shape == (num_design_params, num_features) == b.shape

            self.a = a
            self.a_intercept = a_intercept
            self.a_slope = a_slope
            self.b = b
            self.b_intercept = b_intercept
            self.b_slope = b_slope


# def fetch_batch(indices, X, design):
#     batch_X = tf.gather(X, indices)
#     batch_design = tf.gather(design, indices)
#     return indices, (batch_X, batch_design)


class FullDataModel:
    def __init__(
            self,
            sample_indices: tf.Tensor,
            fetch_fn,
            batch_size: Union[int, tf.Tensor],
            a: tf.Tensor,
            b: tf.Tensor
    ):
        dataset = tf.data.Dataset.from_tensor_slices(sample_indices)

        batched_data = dataset.batch(batch_size)
        batched_data = batched_data.map(fetch_fn)
        batched_data = batched_data.prefetch(1)

        def map_model(idx, data) -> BasicModel:
            data, design = data
            model = BasicModel(data, design, a, b)
            return model

        super()
        model = map_model(*fetch_fn(sample_indices))

        with tf.name_scope("log_likelihood"):
            log_likelihood = op_utils.map_reduce(
                last_elem=tf.gather(sample_indices, tf.size(sample_indices) - 1),
                data=batched_data,
                map_fn=lambda idx, data: map_model(idx, data).log_likelihood,
                parallel_iterations=1,
            )

        with tf.name_scope("loss"):
            loss = -op_utils.map_reduce(
                last_elem=tf.gather(sample_indices, tf.size(sample_indices) - 1),
                data=batched_data,
                map_fn=lambda idx, data: map_model(idx, data).log_likelihood,
                parallel_iterations=1,
            )
            loss = loss / tf.cast(tf.size(sample_indices), dtype=loss.dtype)

        self.X = model.X
        self.design = model.design

        self.dist_estim = model.dist_estim
        self.mu_estim = model.mu_estim
        self.r_estim = model.r_estim
        self.sigma2_estim = model.sigma2_estim

        self.dist_obs = model.dist_obs
        self.mu = model.mu
        self.r = model.r
        self.sigma2 = model.sigma2

        self.probs = model.probs
        self.log_probs = model.log_probs

        # custom
        self.sample_indices = sample_indices

        self.log_likelihood = log_likelihood
        self.loss = loss


class EstimatorGraph(TFEstimatorGraph):
    X: tf.Tensor

    mu: tf.Tensor
    sigma2: tf.Tensor
    a: tf.Tensor
    b: tf.Tensor

    def __init__(
            self,
            fetch_fn,
            num_observations,
            num_features,
            num_design_params,
            graph: tf.Graph = None,
            batch_size=500,
            init_a_intercept=None,
            init_a_slopes=None,
            init_b_intercept=None,
            init_b_slopes=None,
            extended_summary=False,
    ):
        super().__init__(graph)
        self.num_observations = num_observations
        self.num_features = num_features
        self.num_design_params = num_design_params
        self.batch_size = batch_size

        # initial graph elements
        with self.graph.as_default():
            # design = tf_ops.caching_placeholder(tf.float32, shape=(num_observations, num_design_params), name="design")

            learning_rate = tf.placeholder(tf.float32, shape=(), name="learning_rate")
            # train_steps = tf.placeholder(tf.int32, shape=(), name="training_steps")

            with tf.name_scope("input_pipeline"):
                data_indices = tf.data.Dataset.from_tensor_slices((
                    tf.range(num_observations, name="sample_index")
                ))
                training_data = data_indices.apply(tf.contrib.data.shuffle_and_repeat(buffer_size=2 * batch_size))
                training_data = training_data.apply(tf.contrib.data.batch_and_drop_remainder(batch_size))
                training_data = training_data.map(fetch_fn)
                training_data = training_data.prefetch(2)

                iterator = training_data.make_one_shot_iterator()
                batch_sample_index, (batch_X, batch_design) = iterator.get_next()

            # Batch model:
            #     only `batch_size` observations will be used;
            #     All per-sample variables have to be passed via `data`.
            #     Sample-independent variables (e.g. per-feature distributions) can be created inside the batch model
            batch_vars = ModelVars(
                batch_X,
                batch_design,
                init_a_intercept=init_a_intercept,
                init_a_slopes=init_a_slopes,
                init_b_intercept=init_b_intercept,
                init_b_slopes=init_b_slopes,
            )

            batch_model = BasicModel(batch_X, batch_design, batch_vars.a, batch_vars.b)

            # minimize negative log probability (log(1) = 0);
            # use the mean loss to keep a constant learning rate independently of the batch size
            loss = -tf.reduce_mean(batch_model.log_probs, name="loss")

            # ### management
            with tf.name_scope("training"):
                trainers = train_utils.MultiTrainer(
                    loss=loss,
                    variables=tf.trainable_variables(),
                    learning_rate=learning_rate
                )
                gradient = trainers.gradient

                aggregated_gradient = tf.add_n(
                    [tf.reduce_sum(tf.abs(grad), axis=0) for (grad, var) in gradient])

            with tf.name_scope("init_op"):
                init_op = tf.global_variables_initializer()

            self.batch_vars = batch_vars
            self.batch_model = batch_model

            self.loss = loss

            self.trainers = trainers
            self.global_step = trainers.global_step

            self.gradient = aggregated_gradient
            self.plain_gradient = gradient

            # self.train_op_GD = train_op_GD
            # self.train_op_Adam = train_op_Adam
            # self.train_op_Adagrad = train_op_Adagrad
            # self.train_op_RMSProp = train_op_RMSProp
            # default train op
            self.train_op = trainers.train_op_GD

            self.init_ops = []
            self.init_op = init_op

            # # ### set up class attributes
            # self.X = X
            # self.design = design

            self.a = batch_vars.a
            self.b = batch_vars.b
            assert (self.a.shape == (num_design_params, num_features))
            assert (self.b.shape == (num_design_params, num_features))

            self.batch_probs = batch_model.probs
            self.batch_log_probs = batch_model.log_probs
            self.batch_log_likelihood = batch_model.log_likelihood

            # ### alternative definitions for custom observations:
            sample_selection = tf.placeholder_with_default(tf.range(num_observations),
                                                           shape=(None,),
                                                           name="sample_selection")
            full_data = FullDataModel(
                sample_indices=sample_selection,
                fetch_fn=fetch_fn,
                batch_size=batch_size,
                a=self.a,
                b=self.b,
            )
            full_gradient_a, full_gradient_b = tf.gradients(full_data.loss, (batch_vars.a, batch_vars.b))
            full_gradient = (
                    tf.reduce_sum(tf.abs(full_gradient_a), axis=0) +
                    tf.reduce_sum(tf.abs(full_gradient_b), axis=0)
            )

            self.sample_selection = sample_selection
            self.full_data = full_data

            self.mu = full_data.mu
            self.r = full_data.r
            self.sigma2 = full_data.sigma2

            self.full_gradient = full_gradient
            self.full_loss = full_data.loss

            with tf.name_scope('summaries'):
                tf.summary.histogram('a_intercept', batch_vars.a_intercept)
                tf.summary.histogram('b_intercept', batch_vars.b_intercept)
                tf.summary.histogram('a_slope', batch_vars.a_slope)
                tf.summary.histogram('b_slope', batch_vars.b_slope)
                tf.summary.scalar('loss', loss)
                tf.summary.scalar('learning_rate', learning_rate)

                if extended_summary:
                    tf.summary.scalar('median_ll',
                                      tf.contrib.distributions.percentile(
                                          tf.reduce_sum(batch_model.log_probs, axis=1),
                                          50.)
                                      )
                    tf.summary.histogram('gradient_a', tf.gradients(loss, batch_vars.a))
                    tf.summary.histogram('gradient_b', tf.gradients(loss, batch_vars.b))
                    tf.summary.histogram("full_gradient", full_gradient)
                    tf.summary.scalar("full_gradient_median", tf.contrib.distributions.percentile(full_gradient, 50.))
                    tf.summary.scalar("full_gradient_mean", tf.reduce_mean(full_gradient))

            self.saver = tf.train.Saver()
            self.merged_summary = tf.summary.merge_all()


class Estimator(AbstractEstimator, MonitoredTFEstimator, metaclass=abc.ABCMeta):
    model: EstimatorGraph

    @classmethod
    def param_shapes(cls) -> dict:
        return ESTIMATOR_PARAMS

    def __init__(self, input_data: InputData,
                 batch_size=500,
                 model=None,
                 graph=None,
                 init_a_intercept=None,
                 init_a_slopes=None,
                 init_b_intercept=None,
                 init_b_slopes=None,
                 extended_summary=False,
                 ):

        if model is None:
            if graph is None:
                graph = tf.Graph()

            self._input_data = input_data

            # ### prepare fetch_fn:
            def fetch_fn(idx):
                X_tensor = tf.py_func(input_data.fetch_X, [idx], tf.float32)
                X_tensor.set_shape(
                    idx.get_shape().as_list() + [input_data.num_features]
                )

                design_tensor = tf.py_func(input_data.fetch_design, [idx], tf.float32)
                design_tensor.set_shape(
                    idx.get_shape().as_list() + [input_data.num_design_params]
                )
                return idx, (X_tensor, design_tensor)

            with graph.as_default():
                # create model
                model = EstimatorGraph(
                    fetch_fn=fetch_fn,
                    num_observations=input_data.num_observations,
                    num_features=input_data.num_features,
                    num_design_params=input_data.num_design_params,
                    batch_size=batch_size,
                    graph=graph,
                    init_a_intercept=init_a_intercept,
                    init_a_slopes=init_a_slopes,
                    init_b_intercept=init_b_intercept,
                    init_b_slopes=init_b_slopes,
                    extended_summary=extended_summary
                )

        MonitoredTFEstimator.__init__(self, model)

    def _scaffold(self):
        with self.model.graph.as_default():
            scaffold = tf.train.Scaffold(
                init_op=self.model.init_op,
                summary_op=self.model.merged_summary,
                saver=self.model.saver,
            )
        return scaffold

    def train(self, *args,
              learning_rate=0.5,
              convergence_criteria="t_test",
              loss_history_size=200,
              stop_at_loss_change=0.05,
              **kwargs):
        super().train(*args,
                      feed_dict={"learning_rate:0": learning_rate},
                      convergence_criteria=convergence_criteria,
                      loss_history_size=loss_history_size,
                      stop_at_loss_change=stop_at_loss_change,
                      **kwargs)

    @property
    def input_data(self):
        return self._input_data

    # @property
    # def mu(self):
    #     return self.to_xarray("mu")
    #
    # @property
    # def r(self):
    #     return self.to_xarray("r")
    #
    # @property
    # def sigma2(self):
    #     return self.to_xarray("sigma2")

    @property
    def a(self):
        return self.to_xarray("a")

    @property
    def b(self):
        return self.to_xarray("b")

    @property
    def batch_loss(self):
        return self.to_xarray("loss")

    @property
    def batch_gradient(self):
        return self.to_xarray("gradient")

    # def probs(self, X=None, sample_indices=None):
    #     if X is None:
    #         if sample_indices is None:
    #             sample_indices = np.arange(self.model.num_observations)
    #         feed_dict = {self.model.sample_selection: sample_indices}
    #
    #         return self.run(self.model.full_data.probs, feed_dict=feed_dict)
    #     else:
    #         return super().probs(X)
    #
    # def log_probs(self, X=None, sample_indices=None):
    #     if X is None:
    #         if sample_indices is None:
    #             sample_indices = np.arange(self.model.num_observations)
    #         feed_dict = {self.model.sample_selection: sample_indices}
    #
    #         return self.run(self.model.full_data.log_probs, feed_dict=feed_dict)
    #     else:
    #         return super().log_probs(X)
    #
    # def log_likelihood(self, X=None, sample_indices=None):
    #     if X is None:
    #         if sample_indices is None:
    #             sample_indices = np.arange(self.model.num_observations)
    #         feed_dict = {self.model.sample_selection: sample_indices}
    #
    #         return self.run(self.model.full_data.log_likelihood, feed_dict=feed_dict)
    #     else:
    #         return super().log_likelihood(X)

    @property
    def loss(self):
        return self._get_unsafe("full_loss")

    @property
    def gradient(self):
        return self._get_unsafe("full_gradient")

    def finalize(self):
        store = XArrayEstimatorStore(self)
        self.close_session()
        return store
