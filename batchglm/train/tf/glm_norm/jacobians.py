import logging

import tensorflow as tf

from .external import JacobiansGLMALL

logger = logging.getLogger(__name__)


class Jacobians(JacobiansGLMALL):

    def _weights_jac_a(
            self,
            X,
            loc,
            scale,
    ):
        if isinstance(X, tf.SparseTensor) or isinstance(X, tf.SparseTensorValue):
            const = tf.multiply(
                tf.sparse.add(X, scale),
                tf.divide(
                    loc,
                    tf.add(loc, scale)
                )
            )
            const = tf.sparse.add(X, -const)
        else:
            const = tf.multiply(
                tf.add(X, scale),
                tf.divide(
                    scale,
                    tf.add(loc, scale)
                )
            )
            const = tf.subtract(X, const)
        return const

    def _weights_jac_b(
            self,
            X,
            loc,
            scale,
    ):
        # Pre-define sub-graphs that are used multiple times:
        scalar_one = tf.constant(1, shape=(), dtype=self.dtype)
        if isinstance(X, tf.SparseTensor) or isinstance(X, tf.SparseTensorValue):
            r_plus_x = tf.sparse.add(X, scale)
        else:
            r_plus_x = scale + X

        r_plus_mu = scale + loc

        # Define graphs for individual terms of constant term of hessian:
        const1 = tf.subtract(
            tf.math.digamma(x=r_plus_x),
            tf.math.digamma(x=scale)
        )
        const2 = tf.negative(r_plus_x / r_plus_mu)
        const3 = tf.add(
            tf.log(scale),
            scalar_one - tf.log(r_plus_mu)
        )
        const = tf.add_n([const1, const2, const3])  # [observations, features]
        const = scale * const

        return const
