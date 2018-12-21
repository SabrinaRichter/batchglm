from typing import List

import tensorflow as tf
# import numpy as np

import logging

from .model import BasicModelGraph, ModelVars

from .external import op_utils
from .external import pkg_constants

logger = logging.getLogger(__name__)


def _coef_invariant_a(
        X,
        mu,
        r,
):
    """
    Compute the coefficient index invariant part of the
    mean model gradient of nb_glm model.

    Below, X are design matrices of the mean (m)
    and dispersion (r) model respectively, Y are the
    observed data. Const is constant across all combinations
    of i and j.
    .. math::

        &J^{m}_{i} = X^m_i*\bigg(Y-(Y+r)*\frac{mu}{mu+r}\bigg) \\
        &const = Y-(Y+r)*\frac{mu}{mu+r} \\
        &J^{m}_{i} = X^m_i*const \\

    :param X: tf.tensor observations x features
        Observation by observation and feature.
    :param mu: tf.tensor observations x features
        Value of mean model by observation and feature.
    :param r: tf.tensor observations x features
        Value of dispersion model by observation and feature.
    :param dtype: dtype
    :return const: tf.tensor observations x features
        Coefficient invariant terms of hessian of
        given observations and features.
    """
    const = tf.multiply(  # [observations, features]
        tf.add(X, r),
        tf.divide(
            mu,
            tf.add(mu, r)
        )
    )
    const = tf.subtract(X, const)
    return const


def _coef_invariant_b(
        X,
        mu,
        r,
):
    """
    Compute the coefficient index invariant part of the
    dispersion model gradient of nb_glm model.

    Below, X are design matrices of the mean (m)
    and dispersion (r) model respectively, Y are the
    observed data. Const is constant across all combinations
    of i and j.
    .. math::

        J{r}_{i} &= X^r_i \\
            &*r*\bigg(psi_0(r+Y)-psi_0(r) \\
            &-\frac{r+Y}{r+mu} \\
            &+log(r)+1-log(r+mu) \bigg) \\
        const = r*\bigg(psi_0(r+Y)-psi_0(r) \\ const1
            &-\frac{r+Y}{r+mu} \\ const2
            &+log(r)+1-log(r+mu) \bigg) \\ const3
        J^{r}_{i} &= X^r_i * const \\

    :param X: tf.tensor observations x features
        Observation by observation and feature.
    :param mu: tf.tensor observations x features
        Value of mean model by observation and feature.
    :param r: tf.tensor observations x features
        Value of dispersion model by observation and feature.
    :return const: tf.tensor observations x features
        Coefficient invariant terms of hessian of
        given observations and features.
    """
    scalar_one = tf.constant(1, shape=(), dtype=X.dtype)
    # Pre-define sub-graphs that are used multiple times:
    r_plus_mu = r + mu
    r_plus_x = r + X
    # Define graphs for individual terms of constant term of hessian:
    const1 = tf.subtract(
        tf.math.digamma(x=r_plus_x),
        tf.math.digamma(x=r)
    )
    const2 = tf.negative(r_plus_x / r_plus_mu)
    const3 = tf.add(
        tf.log(r),
        scalar_one - tf.log(r_plus_mu)
    )
    const = tf.add_n([const1, const2, const3])  # [observations, features]
    const = r * const
    return const


class Jacobians:
    """ Compute the nb_glm model jacobian.
    """
    jac: tf.Tensor
    neg_jac: tf.Tensor

    def __init__(
            self,
            batched_data: tf.data.Dataset,
            sample_indices: tf.Tensor,
            batch_model,
            constraints_loc,
            constraints_scale,
            model_vars: ModelVars,
            dtype,
            mode="analytic",
            iterator=False,
            jac_a=True,
            jac_b=True
    ):
        """ Return computational graph for jacobian based on mode choice.

        :param batched_data:
            Dataset iterator over mini-batches of data (used for training) or tf.Tensors of mini-batch.
        :param sample_indices: Indices of samples to be used.
        :param batch_model: BasicModelGraph instance
            Allows evaluation of jacobian via tf.gradients as it contains model graph.
        :param constraints_loc: Constraints for location model.
            Array with constraints in rows and model parameters in columns.
            Each constraint contains non-zero entries for the a of parameters that
            has to sum to zero. This constraint is enforced by binding one parameter
            to the negative sum of the other parameters, effectively representing that
            parameter as a function of the other parameters. This dependent
            parameter is indicated by a -1 in this array, the independent parameters
            of that constraint (which may be dependent at an earlier constraint)
            are indicated by a 1.
        :param constraints_scale: Constraints for scale model.
            Array with constraints in rows and model parameters in columns.
            Each constraint contains non-zero entries for the a of parameters that
            has to sum to zero. This constraint is enforced by binding one parameter
            to the negative sum of the other parameters, effectively representing that
            parameter as a function of the other parameters. This dependent
            parameter is indicated by a -1 in this array, the independent parameters
            of that constraint (which may be dependent at an earlier constraint)
            are indicated by a 1.
        :param mode: str
            Mode by with which hessian is to be evaluated,
            "analytic" uses a closed form solution of the jacobian,
            "tf" allows for evaluation of the jacobian via the tf.gradients function.
        :param iterator: bool
            Whether an iterator or a tensor (single yield of an iterator) is given
            in.
        :param jac_a: bool
            Wether to compute Jacobian for a parameters. If both jac_a and jac_b are true,
            the entire jacobian is computed in self.jac.
        :param jac_b: bool
            Wether to compute Jacobian for b parameters. If both jac_a and jac_b are true,
            the entire jacobian is computed in self.jac.
        """
        if constraints_loc is not None and mode != "tf":
            raise ValueError("closed form jacobians do not work if constraints_loc is not None")
        if constraints_scale is not None and mode != "tf":
            raise ValueError("closed form jacobians do not work if constraints_scale is not None")

        logger.debug("jacobian mode: %s" % mode)
        logger.debug("compute jacobian for a model: %s" % str(jac_a))
        logger.debug("compute jacobian for b model: %s" % str(jac_b))

        self._compute_jac_a = jac_a
        self._compute_jac_b = jac_b

        if mode == "analytic":
            J = self._analytic(
                batched_data=batched_data,
                sample_indices=sample_indices,
                constraints_loc=constraints_loc,
                constraints_scale=constraints_scale,
                model_vars=model_vars,
                iterator=iterator,
                dtype=dtype
            )
        elif mode == "tf":
            # Tensorflow computes the jacobian based on the objective,
            # which is the negative log-likelihood. Accordingly, the jacobian
            # is the negative jacobian computed here.
            J = self._tf(
                batched_data=batched_data,
                sample_indices=sample_indices,
                batch_model=batch_model,
                constraints_loc=constraints_loc,
                constraints_scale=constraints_scale,
                model_vars=model_vars,
                iterator=iterator,
                dtype=dtype
            )
        else:
            raise ValueError("mode not recognized in Jacobian: " + mode)

        # Assign jacobian blocks.
        p_shape_a = model_vars.a_var.shape[0]  # This has to be _var to work with constraints.
        if self._compute_jac_a and self._compute_jac_b:
            J_a = J[:, :p_shape_a]
            J_b = J[:, p_shape_a:]
            negJ = tf.negative(J)
            negJ_a = tf.negative(J_a)
            negJ_b = tf.negative(J_b)
        elif self._compute_jac_a and not self._compute_jac_b:
            J_a = J
            J_b = None
            J = J
            negJ = tf.negative(J)
            negJ_a = tf.negative(J_a)
            negJ_b = None
        elif not self._compute_jac_a and self._compute_jac_b:
            J_a = None
            J_b = J
            J = J
            negJ = tf.negative(J)
            negJ_a = None
            negJ_b = tf.negative(J_b)
        else:
            raise ValueError("either require jac_a or jac_b")

        self.jac = J
        self.jac_a = J_a
        self.jac_b = J_b
        self.neg_jac = negJ
        self.neg_jac_a = negJ_a
        self.neg_jac_b = negJ_b

    def _analytic(
            self,
            batched_data,
            sample_indices,
            constraints_loc,
            constraints_scale,
            model_vars: ModelVars,
            iterator,
            dtype
    ):
        """
        Compute the closed-form of the nb_glm model jacobian
        by evalutating its terms grouped by observations.
        """

        def _a_byobs(X, design_loc, design_scale, mu, r):
            """
            Compute the mean model block of the jacobian.

            :param X: tf.tensor observations x features
                Observation by observation and feature.
            :param mu: tf.tensor observations x features
                Value of mean model by observation and feature.
            :param r: tf.tensor observations x features
                Value of dispersion model by observation and feature.
            :return Jblock: tf.tensor features x coefficients
                Block of jacobian.
            """
            const = _coef_invariant_a(X=X, mu=mu, r=r)  # [observations, features]
            Jblock = tf.matmul(tf.transpose(const), design_loc)  # [features, coefficients]
            return Jblock

        def _b_byobs(X, design_loc, design_scale, mu, r):
            """
            Compute the dispersion model block of the jacobian.
            """
            const = _coef_invariant_b(X=X, mu=mu, r=r)  # [observations, features]
            Jblock = tf.matmul(tf.transpose(const), design_scale)  # [features, coefficients]
            return Jblock

        def _assemble_bybatch(idx, data):
            """
            Assemble jacobian of a batch of observations across all features.

            This function runs the data batch (an observation) through the
            model graph and calls the wrappers that compute the
            individual closed forms of the jacobian.

            :param data: tuple
                Containing the following parameters:
                - X: tf.tensor observations x features
                    Observation by observation and feature.
                - size_factors: tf.tensor observations x features
                    Model size factors by observation and feature.
                - params: tf.tensor features x coefficients
                    Estimated model variables.
            :return J: tf.tensor features x coefficients
                Jacobian evaluated on a single observation, provided in data.
            """
            X, design_loc, design_scale, size_factors = data

            model = BasicModelGraph(
                X=X,
                design_loc=design_loc,
                design_scale=design_scale,
                constraints_loc=constraints_loc,
                constraints_scale=constraints_scale,
                a=model_vars.a,
                b=model_vars.b,
                dtype=dtype,
                size_factors=size_factors
            )
            mu = model.mu
            r = model.r

            if self._compute_jac_a and self._compute_jac_b:
                J_a = _a_byobs(X=X, design_loc=design_loc, design_scale=design_scale, mu=mu, r=r)
                J_b = _b_byobs(X=X, design_loc=design_loc, design_scale=design_scale, mu=mu, r=r)
                J = tf.concat([J_a, J_b], axis=1)
            elif self._compute_jac_a and not self._compute_jac_b:
                J = _a_byobs(X=X, design_loc=design_loc, design_scale=design_scale, mu=mu, r=r)
            elif not self._compute_jac_a and self._compute_jac_b:
                J = _b_byobs(X=X, design_loc=design_loc, design_scale=design_scale, mu=mu, r=r)
            else:
                raise ValueError("either require jac_a or jac_b")

            return J

        def _red(prev, cur):
            """
            Reduction operation for jacobian computation across observations.

            Every evaluation of the jacobian on an observation yields a full
            jacobian matrix. This function sums over consecutive evaluations
            of this hessian so that not all seperate evluations have to be
            stored.
            """
            return tf.add(prev, cur)

        if iterator:
            J = op_utils.map_reduce(
                last_elem=tf.gather(sample_indices, tf.size(sample_indices) - 1),
                data=batched_data,
                map_fn=_assemble_bybatch,
                reduce_fn=_red,
                parallel_iterations=pkg_constants.TF_LOOP_PARALLEL_ITERATIONS
            )
        else:
            J = _assemble_bybatch(
                idx=sample_indices,
                data=batched_data
            )

        return J

    def _tf(
            self,
            batched_data,
            sample_indices,
            batch_model,
            constraints_loc,
            constraints_scale,
            model_vars: ModelVars,
            iterator,
            dtype
    ) -> List[tf.Tensor]:
        """
        Compute jacobian via tf.gradients.
        """

        def _jac(batch_model, model_vars):
            J = tf.gradients(batch_model.log_likelihood, model_vars.params)[0]
            J = tf.transpose(J)
            return J

        def _jac_a(batch_model, model_vars):
            J_a = tf.gradients(batch_model.log_likelihood, model_vars.a_var)[0]
            J_a = tf.transpose(J_a)
            return J_a

        def _jac_b(batch_model, model_vars):
            J_b = tf.gradients(batch_model.log_likelihood, model_vars.b_var)[0]
            J_b = tf.transpose(J_b)
            return J_b

        def _assemble_bybatch(idx, data):
            """
            Assemble jacobian of a batch of observations across all features.

            This function runs the data batch (an observation) through the
            model graph and calls the wrappers that compute the
            individual closed forms of the jacobian.

            :param data: tuple
                Containing the following parameters:
                - X: tf.tensor observations x features
                    Observation by observation and feature.
                - size_factors: tf.tensor observations x features
                    Model size factors by observation and feature.
                - params: tf.tensor features x coefficients
                    Estimated model variables.
            :return J: tf.tensor features x coefficients
                Jacobian evaluated on a single observation, provided in data.
            """
            X, design_loc, design_scale, size_factors = data

            model = BasicModelGraph(
                X=X,
                design_loc=design_loc,
                design_scale=design_scale,
                constraints_loc=constraints_loc,
                constraints_scale=constraints_scale,
                a=model_vars.a,
                b=model_vars.b,
                dtype=dtype,
                size_factors=size_factors
            )

            if self._compute_jac_a and self._compute_jac_b:
                J = _jac(batch_model=model, model_vars=model_vars)
            elif self._compute_jac_a and not self._compute_jac_b:
                J = _jac_a(batch_model=model, model_vars=model_vars)
            elif not self._compute_jac_a and self._compute_jac_b:
                J = _jac_b(batch_model=model, model_vars=model_vars)
            else:
                raise ValueError("either require jac_a or jac_b")
            return J

        def _red(prev, cur):
            """
            Reduction operation for jacobian computation across observation batches.

            Every evaluation of the jacobian on an observation yields a full
            jacobian matrix. This function sums over consecutive evaluations
            of this hessian so that not all seperate evluations have to be
            stored.
            """
            return tf.add(prev, cur)

        if iterator == True and batch_model is None:
            J = op_utils.map_reduce(
                last_elem=tf.gather(sample_indices, tf.size(sample_indices) - 1),
                data=batched_data,
                map_fn=_assemble_bybatch,
                reduce_fn=_red,
                parallel_iterations=pkg_constants.TF_LOOP_PARALLEL_ITERATIONS
            )
        elif iterator == False and batch_model is None:
            J = _assemble_bybatch(
                idx=sample_indices,
                data=batched_data
            )
        else:
            if self._compute_jac_a and self._compute_jac_b:
                J = _jac(batch_model=batch_model, model_vars=model_vars)
            elif self._compute_jac_a and not self._compute_jac_b:
                J = _jac_a(batch_model=batch_model, model_vars=model_vars)
            elif not self._compute_jac_a and self._compute_jac_b:
                J = _jac_b(batch_model=batch_model, model_vars=model_vars)
            else:
                raise ValueError("either require jac_a or jac_b")

        return J
