import abc
import logging
from typing import Union

import numpy as np
import tensorflow as tf
import xarray as xr

try:
    import anndata
except ImportError:
    anndata = None

from .model import ModelVarsGLM, BasicModelGraphGLM
from .external import TFEstimatorGraph
from .external import train_utils
from .external import pkg_constants

logger = logging.getLogger(__name__)


class FullDataModelGraphGLM:
    """
    Computational graph to evaluate model on full data set.

    Here, we assume that the model cannot be executed on the full data set
    for memory reasons and therefore divide the data set into batches,
    execute the model on these batches and summarise the resulting metrics
    across batches. FullDataModelGraph is therefore an extension of
    BasicModelGraph that distributes operations across batches of observations.

    The distribution is performed by the function map_model().
    The model metrics which can be collected are:

        - The model likelihood (cost function value).
        - Model Jacobian matrix for trainer parameters (for training).
        - Model Jacobian matrix for all parameters (for downstream usage,
        e.g. hypothesis tests which can also be performed on closed form MLEs).
        - Model Hessian matrix for trainer parameters (for training).
        - Model Hessian matrix for all parameters (for downstream usage,
        e.g. hypothesis tests which can also be performed on closed form MLEs).
    """
    log_likelihood: tf.Tensor
    norm_log_likelihood: tf.Tensor
    norm_neg_log_likelihood: tf.Tensor
    loss: tf.Tensor

    jac: tf.Tensor
    jac_train: tf.Tensor

    hessians: tf.Tensor
    hessians_train: tf.Tensor

    fim: tf.Tensor
    fim_train: tf.Tensor

    noise_model: str


class BatchedDataModelGraphGLM:
    """
    Computational graph to evaluate model on batches of data set.

    The model metrics of a batch which can be collected are:

        - The model likelihood (cost function value).
        - Model Jacobian matrix for trained parameters (for training).
        - Model Hessian matrix for trained parameters (for training).
        - Model Fisher information matrix for trained parameters (for training).
    """
    log_likelihood: tf.Tensor
    norm_log_likelihood: tf.Tensor
    norm_neg_log_likelihood: tf.Tensor
    loss: tf.Tensor

    jac_train: tf.Tensor
    hessians_train: tf.Tensor
    fim_train: tf.Tensor

    noise_model: str


class GradientGraphGLM:
    """

    Define newton-rhapson updates and gradients depending on termination type
    and depending on whether data is batched.
    The formed have to be distinguished because gradients and parameter updates
    are set to zero (or not computed in the case of newton-raphson) for
    converged features if feature-wise termination is chosen.
    The latter have to be distinguished as there are different jacobians
    and hessians for the full and the batched data.
    """
    model_vars: tf.Tensor
    full_data_model: tf.Tensor
    batched_data_model: tf.Tensor
    batch_jac: tf.Tensor

    nr_update_full: Union[tf.Tensor, None]
    nr_update_batched: Union[tf.Tensor, None]

    def __init__(
            self,
            termination_type,
            train_loc,
            train_scale
    ):
        if train_loc or train_scale:
            if termination_type == "by_feature":
                logger.debug(" ** Build gradients for training graph: by_feature")
                self.gradients_full_byfeature()
                self.gradients_batched_byfeature()
            elif termination_type == "global":
                logger.debug(" ** Build gradients for training graph: global")
                self.gradients_full_global()
                self.gradients_batched_global()
            else:
                raise ValueError("convergence_type %s not recognized." % termination_type)

            # Pad gradients to receive update tensors that match
            # the shape of model_vars.params.
            if train_loc:
                if train_scale:
                    gradients_batch = self.gradients_batch_raw
                    gradients_full = self.gradients_full_raw
                else:
                    gradients_batch = tf.concat([
                        self.gradients_batch_raw,
                        tf.zeros_like(self.model_vars.b_var)
                    ], axis=0)
                    gradients_full = tf.concat([
                        self.gradients_full_raw,
                        tf.zeros_like(self.model_vars.b_var)
                    ], axis=0)
            elif train_scale:
                gradients_batch = tf.concat([
                    tf.zeros_like(self.model_vars.a_var),
                    self.gradients_batch_raw
                ], axis=0)
                gradients_full = tf.concat([
                    tf.zeros_like(self.model_vars.a_var),
                    self.gradients_full_raw
                ], axis=0)
        else:
            # These gradients are returned for convergence evaluation.
            # In this case, closed form estimates were used, one could
            # still evaluate the gradients here but we do not do
            # this to speed up run time.
            gradients_batch = tf.zeros_like(self.model_vars.params)
            gradients_full = tf.zeros_like(self.model_vars.params)

        self.gradients_full = gradients_full
        self.gradients_batch = gradients_batch

    def gradients_full_byfeature(self):
        gradients_full_all = tf.transpose(self.full_data_model.jac_train.neg_jac)
        gradients_full = tf.concat([
            # tf.gradients(full_data_model.norm_neg_log_likelihood,
            #             model_vars.params_by_gene[i])[0]
            tf.expand_dims(gradients_full_all[:, i], axis=-1)
            if not self.model_vars.converged[i]
            else tf.zeros([param_grad_vec.shape[1], 1], dtype=dtype)
            for i in range(self.model_vars.n_features)
        ], axis=1)

        self.gradients_full_raw = gradients_full

    def gradients_batched_byfeature(self):
        gradients_batch_all = tf.transpose(self.batched_data_model.jac_train.neg_jac)
        gradients_batch = tf.concat([
            # tf.gradients(batch_model.norm_neg_log_likelihood,
            #             model_vars.params_by_gene[i])[0]
            tf.expand_dims(gradients_batch_all[:, i], axis=-1)
            if not self.model_vars.converged[i]
            else tf.zeros([param_grad_vec.shape[1], 1], dtype=dtype)
            for i in range(self.model_vars.n_features)
        ], axis=1)

        self.gradients_batch_raw = gradients_batch

    def gradients_full_global(self):
        gradients_full = tf.transpose(self.full_data_model.jac_train.neg_jac)
        self.gradients_full_raw = gradients_full

    def gradients_batched_global(self):
        gradients_batch = tf.transpose(self.batched_data_model.jac_train.neg_jac)
        self.gradients_batch_raw = gradients_batch


class NewtonGraphGLM:
    """
    Define update vectors which require a matrix inversion: Newton-Raphson and
    IRLS updates.

    Define newton-type updates and gradients depending on termination type
    and depending on whether data is batched.
    The former have to be distinguished because gradients and parameter updates
    are set to zero (or not computed in the case of newton-raphson) for
    converged features if feature-wise termination is chosen.
    The latter have to be distinguished as there are different jacobians
    and hessians for the full and the batched data.
    """
    model_vars: tf.Tensor
    full_data_model: tf.Tensor
    batched_data_model: tf.Tensor
    batch_jac: tf.Tensor
    batch_hessians: tf.Tensor

    nr_update_full: Union[tf.Tensor, None]
    nr_update_batched: Union[tf.Tensor, None]
    irls_update_full: Union[tf.Tensor, None]
    irls_update_batched: Union[tf.Tensor, None]

    idx_nonconverged: np.ndarray

    def __init__(
            self,
            termination_type,
            provide_optimizers,
            train_mu,
            train_r
    ):
        if train_mu or train_r:
            if provide_optimizers["nr"]:
                nr_update_full_raw, nr_update_batched_raw = self.build_updates(
                    full_lhs=self.full_data_model.hessians_train.neg_hessian,
                    batched_lhs=self.batched_data_model.hessians_train.neg_hessian,
                    full_rhs=self.full_data_model.jac_train.neg_jac,
                    batched_rhs=self.batched_data_model.jac_train.neg_jac,
                    termination_type=termination_type,
                    psd=False
                )
                nr_update_full, nr_update_batched = self.pad_updates(
                    train_mu=train_mu,
                    train_r=train_r,
                    update_full_raw=nr_update_full_raw,
                    update_batched_raw=nr_update_batched_raw
                )
            else:
                nr_update_full = None
                nr_update_batched = None

            if provide_optimizers["irls"]:
                # Compute a and b model updates separately.
                if train_mu:
                    # The FIM of the mean model is guaranteed to be
                    # positive semi-definite and can therefore be inverted
                    # with the Cholesky decomposition. This information is
                    # passed here with psd=True.
                    irls_update_a_full, irls_update_a_batched = self.build_updates(
                        full_lhs=self.full_data_model.fim_train.fim_a,
                        batched_lhs=self.batched_data_model.fim_train.fim_a,
                        full_rhs=self.full_data_model.jac_train.neg_jac_a,
                        batched_rhs=self.batched_data_model.jac_train.neg_jac_a,
                        termination_type=termination_type,
                        psd=True
                    )
                else:
                    irls_update_a_full = None
                    irls_update_a_batched = None

                if train_r:
                    irls_update_b_full, irls_update_b_batched = self.build_updates(
                        full_lhs=self.full_data_model.fim_train.fim_b,
                        batched_lhs=self.batched_data_model.fim_train.fim_b,
                        full_rhs=self.full_data_model.jac_train.neg_jac_b,
                        batched_rhs=self.batched_data_model.jac_train.neg_jac_b,
                        termination_type=termination_type,
                        psd=True  # TODO proove
                    )
                else:
                    irls_update_b_full = None
                    irls_update_b_batched = None

                if train_mu and train_r:
                    irls_update_full_raw = tf.concat([irls_update_a_full, irls_update_b_full], axis=0)
                    irls_update_batched_raw = tf.concat([irls_update_a_batched, irls_update_b_batched], axis=0)
                elif train_mu:
                    irls_update_full_raw = irls_update_a_full
                    irls_update_batched_raw = irls_update_a_batched
                elif train_r:
                    irls_update_full_raw = irls_update_b_full
                    irls_update_batched_raw = irls_update_b_batched
                else:
                    irls_update_full_raw = None
                    irls_update_batched_raw = None

                irls_update_full, irls_update_batched = self.pad_updates(
                    train_mu=train_mu,
                    train_r=train_r,
                    update_full_raw=irls_update_full_raw,
                    update_batched_raw=irls_update_batched_raw
                )
            else:
                irls_update_full = None
                irls_update_batched = None
        else:
            nr_update_full = None
            nr_update_batched = None
            irls_update_full = None
            irls_update_batched = None

        self.nr_update_full = nr_update_full
        self.nr_update_batched = nr_update_batched
        self.irls_update_full = irls_update_full
        self.irls_update_batched = irls_update_batched

    def build_updates(
            self,
            full_lhs,
            batched_rhs,
            full_rhs,
            batched_lhs,
            termination_type: str,
            psd
    ):
        if termination_type == "by_feature":
            update_full = self.newton_type_update_full_byfeature(
                lhs=full_lhs,
                rhs=full_rhs,
                psd=psd
            )
            update_batched = self.newton_type_update_batched_byfeature(
                lhs=batched_lhs,
                rhs=batched_rhs,
                psd=psd
            )
        elif termination_type == "global":
            update_full = self.newton_type_update_full_global(
                lhs=full_lhs,
                rhs=full_rhs,
                psd=psd
            )
            update_batched = self.newton_type_update_batched_global(
                lhs=batched_lhs,
                rhs=batched_rhs,
                psd=psd
            )
        else:
            raise ValueError("convergence_type %s not recognized." % termination_type)

        return update_full, update_batched

    def pad_updates(
            self,
            update_full_raw,
            update_batched_raw,
            train_mu,
            train_r
    ):
        # Pad update vectors to receive update tensors that match
        # the shape of model_vars.params.
        if train_mu:
            if train_r:
                netwon_type_update_full = update_full_raw
                newton_type_update_batched = update_batched_raw
            else:
                netwon_type_update_full = tf.concat([
                    update_full_raw,
                    tf.zeros_like(self.model_vars.b_var)
                ], axis=0)
                newton_type_update_batched = tf.concat([
                    update_batched_raw,
                    tf.zeros_like(self.model_vars.b_var)
                ], axis=0)
        elif train_r:
            netwon_type_update_full = tf.concat([
                tf.zeros_like(self.model_vars.a_var),
                update_full_raw
            ], axis=0)
            newton_type_update_batched = tf.concat([
                tf.zeros_like(self.model_vars.a_var),
                update_batched_raw
            ], axis=0)
        else:
            raise ValueError("No training necessary")

        return netwon_type_update_full, newton_type_update_batched

    def newton_type_update_full_byfeature(
            self,
            lhs,
            rhs,
            psd
    ):
        # Compute parameter update for non-converged gene only.
        delta_t_bygene_nonconverged = tf.squeeze(tf.matrix_solve_ls(
            tf.gather(
                lhs,
                indices=self.idx_nonconverged,
                axis=0),
            tf.expand_dims(
                tf.gather(
                    rhs,
                    indices=self.idx_nonconverged,
                    axis=0)
                , axis=-1),
            fast=psd and pkg_constants.CHOLESKY_LSTSQS
        ), axis=-1)
        # Write parameter updates into matrix of size of all parameters which
        # contains zero entries for updates of already converged genes.
        delta_t_bygene_full = tf.concat([
            tf.gather(delta_t_bygene_nonconverged,
                      indices=np.where(self.idx_nonconverged == i)[0],
                      axis=0)
            if not self.model_vars.converged[i]
            else tf.zeros([1, rhs.shape[1]])
            for i in range(self.model_vars.n_features)
        ], axis=0)
        nr_update_full = tf.transpose(delta_t_bygene_full)

        return nr_update_full

    def newton_type_update_batched_byfeature(
            self,
            lhs,
            rhs,
            psd
    ):
        # Compute parameter update for non-converged gene only.
        delta_batched_t_bygene_nonconverged = tf.squeeze(tf.matrix_solve_ls(
            tf.gather(
                lhs,
                indices=self.idx_nonconverged,
                axis=0),
            tf.expand_dims(
                tf.gather(
                    rhs,
                    indices=self.idx_nonconverged,
                    axis=0)
                , axis=-1),
            fast=psd and pkg_constants.CHOLESKY_LSTSQS
        ), axis=-1)
        # Write parameter updates into matrix of size of all parameters which
        # contains zero entries for updates of already converged genes.
        delta_batched_t_bygene_full = tf.concat([
            tf.gather(delta_batched_t_bygene_nonconverged,
                      indices=np.where(self.idx_nonconverged == i)[0],
                      axis=0)
            if not self.model_vars.converged[i]
            else tf.zeros([1, rhs.shape[1]])
            for i in range(self.model_vars.n_features)
        ], axis=0)
        nr_update_batched = tf.transpose(delta_batched_t_bygene_full)

        return nr_update_batched

    def newton_type_update_full_global(
            self,
            lhs,
            rhs,
            psd
    ):
        delta_t = tf.squeeze(tf.matrix_solve_ls(
            lhs,
            # (full_data_model.hessians + tf.transpose(full_data_model.hessians, perm=[0, 2, 1])) / 2,  # symmetrization, don't need this with closed forms
            tf.expand_dims(rhs, axis=-1),
            fast=psd and pkg_constants.CHOLESKY_LSTSQS
        ), axis=-1)
        nr_update_full = tf.transpose(delta_t)

        return nr_update_full

    def newton_type_update_batched_global(
            self,
            lhs,
            rhs,
            psd
    ):
        delta_batched_t = tf.squeeze(tf.matrix_solve_ls(
            lhs,
            tf.expand_dims(rhs, axis=-1),
            fast=psd and pkg_constants.CHOLESKY_LSTSQS
        ), axis=-1)
        nr_update_batched = tf.transpose(delta_batched_t)

        return nr_update_batched


class TrainerGraphGLM:
    """

    """
    model_vars: ModelVarsGLM
    full_data_model: FullDataModelGraphGLM
    batched_data_model: BasicModelGraphGLM
    gradients_batch: tf.Tensor
    gradients_full: tf.Tensor
    nr_update_full: tf.Tensor
    nr_update_batched: tf.Tensor
    irls_update_full: tf.Tensor
    irls_update_batched: tf.Tensor

    gradient: tf.Tensor
    full_gradient: tf.Tensor

    num_observations: int
    num_features: int
    num_design_loc_params: int
    num_design_scale_params: int
    num_loc_params: int
    num_scale_params: int
    batch_size: int

    def __init__(
            self,
            provide_optimizers,
            train_loc,
            train_scale,
            dtype
    ):
        with tf.name_scope("training_graphs"):
            global_step = tf.train.get_or_create_global_step()

            # Create trainers that produce training operations.
            if train_loc or train_scale:
                trainer_batch = train_utils.MultiTrainer(
                    variables=self.model_vars.params,
                    gradients=self.gradients_batch,
                    newton_delta=self.nr_update_batched,
                    irls_delta=self.irls_update_batched,
                    learning_rate=self.learning_rate,
                    global_step=global_step,
                    apply_gradients=lambda grad: tf.where(tf.is_nan(grad), tf.zeros_like(grad), grad),
                    provide_optimizers=provide_optimizers,
                    name="batch_data_trainers"
                )
                batch_gradient = trainer_batch.plain_gradient_by_variable(self.model_vars.params)
                batch_gradient = tf.reduce_sum(tf.abs(batch_gradient), axis=0)
            else:
                trainer_batch = None
                batch_gradient = None

            if train_loc or train_scale:
                trainer_full = train_utils.MultiTrainer(
                    variables=self.model_vars.params,
                    gradients=self.gradients_full,
                    newton_delta=self.nr_update_full,
                    irls_delta=self.irls_update_full,
                    learning_rate=self.learning_rate,
                    global_step=global_step,
                    apply_gradients=lambda grad: tf.where(tf.is_nan(grad), tf.zeros_like(grad), grad),
                    provide_optimizers=provide_optimizers,
                    name="full_data_trainers"
                )
                full_gradient = trainer_full.plain_gradient_by_variable(self.model_vars.params)
                full_gradient = tf.reduce_sum(tf.abs(full_gradient), axis=0)
            else:
                trainer_full = None
                full_gradient = None

        # # ### BFGS implementation using SciPy L-BFGS
        # with tf.name_scope("bfgs"):
        #     feature_idx = tf.placeholder(dtype="int64", shape=())
        #
        #     X_s = tf.gather(X, feature_idx, axis=1)
        #     a_s = tf.gather(a, feature_idx, axis=1)
        #     b_s = tf.gather(b, feature_idx, axis=1)
        #
        #     model = BasicModelGraph(X_s, design_loc, design_scale, a_s, b_s, size_factors=size_factors)
        #
        #     trainer = tf.contrib.opt.ScipyOptimizerInterface(
        #         model.loss,
        #         method='L-BFGS-B',
        #         options={'maxiter': maxiter})

        self.global_step = global_step

        self.trainer_batch = trainer_batch
        self.gradient = batch_gradient

        self.trainer_full = trainer_full
        self.full_gradient = full_gradient

        self.train_op = None

    @abc.abstractmethod
    def param_bounds(self):
        pass


class EstimatorGraphGLM(TFEstimatorGraph, GradientGraphGLM, NewtonGraphGLM, TrainerGraphGLM):
    """
    The estimator graphs are all graph necessary to perform parameter updates and to
    summarise a current parameter estimate.

    The estimator graph class is divided into the following major sub graphs:

        - The input pipeline: Feed data for parameter updates.
        -
    """
    X: tf.Tensor

    a_var: tf.Tensor
    b_var: tf.Tensor

    model_vars = ModelVarsGLM

    noise_model: str

    def __init__(
            self,
            num_observations,
            num_features,
            num_design_loc_params,
            num_design_scale_params,
            num_loc_params,
            num_scale_params,
            graph: tf.Graph,
            batch_size: int,
            constraints_loc: xr.DataArray,
            constraints_scale: xr.DataArray,
            dtype
    ):
        """

        :param num_observations: int
            Number of observations.
        :param num_features: int
            Number of features.
        :param num_design_loc_params: int
            Number of parameters per feature in mean model.
        :param num_design_scale_params: int
            Number of parameters per feature in scale model.
        :param graph: tf.Graph
        :param constraints_loc: tensor (all parameters x dependent parameters)
            Tensor that encodes how complete parameter set which includes dependent
            parameters arises from indepedent parameters: all = <constraints, indep>.
            This tensor describes this relation for the mean model.
            This form of constraints is used in vector generalized linear models (VGLMs).
        :param constraints_scale: tensor (all parameters x dependent parameters)
            Tensor that encodes how complete parameter set which includes dependent
            parameters arises from indepedent parameters: all = <constraints, indep>.
            This tensor describes this relation for the dispersion model.
            This form of constraints is used in vector generalized linear models (VGLMs).
        """
        TFEstimatorGraph.__init__(
            self=self,
            graph=graph
        )

        self.num_observations = num_observations
        self.num_features = num_features
        self.num_design_loc_params = num_design_loc_params
        self.num_design_scale_params = num_design_scale_params
        self.num_loc_params = num_loc_params
        self.num_scale_params = num_scale_params
        self.batch_size = batch_size

        self.constraints_loc = self._set_constraints(
            constraints=constraints_loc,
            num_design_params=self.num_design_loc_params,
            dtype=dtype
        )
        self.constraints_scale = self._set_constraints(
            constraints=constraints_scale,
            num_design_params=self.num_design_scale_params,
            dtype=dtype
        )

        self.learning_rate = tf.placeholder(dtype, shape=(), name="learning_rate")

    def _run_trainer_init(
            self,
            termination_type,
            provide_optimizers,
            train_loc,
            train_scale,
            dtype
    ):
        GradientGraphGLM.__init__(
            self=self,
            termination_type=termination_type,
            train_loc=train_loc,
            train_scale=train_scale
        )
        NewtonGraphGLM.__init__(
            self=self,
            termination_type=termination_type,
            provide_optimizers=provide_optimizers,
            train_mu=train_loc,
            train_r=train_scale
        )
        TrainerGraphGLM.__init__(
            self=self,
            provide_optimizers=provide_optimizers,
            train_loc=train_loc,
            train_scale=train_scale,
            dtype=dtype
        )

        with tf.name_scope("init_op"):
            self.init_op = tf.global_variables_initializer()
            self.init_ops = []

    def _set_out_var(
            self,
            feature_isnonzero,
            dtype
    ):
        # ### output values:
        #       override all-zero features with lower bound coefficients
        with tf.name_scope("output"):
            logger.debug(" ** Build training graph: output")
            bounds_min, bounds_max = self.param_bounds(dtype)

            param_nonzero_a_var = tf.broadcast_to(feature_isnonzero, [self.num_loc_params, self.num_features])
            alt_a = tf.broadcast_to(bounds_min["a_var"], [self.num_loc_params, self.num_features])
            a_var = tf.where(
                param_nonzero_a_var,
                self.model_vars.a_var,
                alt_a
            )

            param_nonzero_b_var = tf.broadcast_to(feature_isnonzero, [self.num_scale_params, self.num_features])
            alt_b = tf.broadcast_to(bounds_min["b_var"], [self.num_scale_params, self.num_features])
            b_var = tf.where(
                param_nonzero_b_var,
                self.model_vars.b_var,
                alt_b
            )

        self.a_var = a_var
        self.b_var = b_var

    def _set_constraints(
            self,
            constraints,
            num_design_params,
            dtype
    ):
        if constraints is None:
            return tf.eye(
                num_rows=tf.constant(num_design_params, shape=(), dtype="int32"),
                dtype=dtype
            )
        else:
            assert constraints.shape[0] == num_design_params, "constraint dimension mismatch"
            return tf.cast(constraints, dtype=dtype)

    @abc.abstractmethod
    def param_bounds(self):
        pass
