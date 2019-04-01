import numpy as np

from .model import Model
from .external import rand_utils, _Simulator_GLM


class Simulator(_Simulator_GLM, Model):
    """
    Simulator for Generalized Linear Models (GLMs) with beta distributed noise.
    Uses the natural logarithm as linker function.
    """

    def __init__(
            self,
            num_observations=1000,
            num_features=100
    ):
        Model.__init__(self)
        _Simulator_GLM.__init__(
            self,
            num_observations=num_observations,
            num_features=num_features
        )

    def generate_params(
            self,
            rand_fn_ave=lambda shape: np.random.uniform(10, 20, shape),
            rand_fn=lambda shape: np.random.uniform(1, 1, shape),
            rand_fn_loc=None,
            rand_fn_scale=None,
        ):
        def fn_scale(shape):
            theta = np.ones(shape)
            theta[0, :] = np.random.uniform(40, 80, shape[1])
            return theta

        self._generate_params(
            self,
            rand_fn_ave=rand_fn_ave,
            rand_fn=rand_fn,
            rand_fn_loc=rand_fn_loc,
            rand_fn_scale=fn_scale,
        )

    def generate_data(self):
        """
        Sample random data based on negative binomial distribution and parameters.
        """
        self.data["X"] = (
            self.param_shapes()["X"],
            rand_utils.Beta(p=self.p, q=self.q).sample()
        )