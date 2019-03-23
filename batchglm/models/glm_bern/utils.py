from typing import Union

import numpy as np
import xarray as xr

from .external import closedform_glm_mean, closedform_glm_scale
from .external import SparseXArrayDataArray


def closedform_bern_glm_logitmu(
        X: Union[xr.DataArray, SparseXArrayDataArray],
        design_loc,
        constraints_loc,
        size_factors=None,
        link_fn=lambda data: np.log(data/(1-data)),
        inv_link_fn=lambda data: 1/(1+np.exp(-data))
):
    r"""
    Calculates a closed-form solution for the `mu` parameters of bernoulli GLMs.

    :param X: The sample data
    :param design_loc: design matrix for location
    :param constraints_loc: tensor (all parameters x dependent parameters)
        Tensor that encodes how complete parameter set which includes dependent
        parameters arises from indepedent parameters: all = <constraints, indep>.
        This form of constraints is used in vector generalized linear models (VGLMs).
    :param size_factors: size factors for X
    :return: tuple: (groupwise_means, mu, rmsd)
    """
    return closedform_glm_mean(
        X=X,
        dmat=design_loc,
        constraints=constraints_loc,
        size_factors=size_factors,
        link_fn=link_fn,
        inv_link_fn=inv_link_fn
    )

#Todo
# def closedform_nb_glm_logphi(
#         X: Union[xr.DataArray, SparseXArrayDataArray],
#         design_scale: xr.DataArray,
#         constraints=None,
#         size_factors=None,
#         groupwise_means=None,
#         link_fn=np.log
# ):
#     r"""
#     Calculates a closed-form solution for the log-scale parameters of negative-binomial GLMs.
#     Based on the Method-of-Moments estimator.
#
#     :param X: The sample data
#     :param design_scale: design matrix for scale
#     :param constraints: some design constraints
#     :param size_factors: size factors for X
#     :param groupwise_means: optional, in case if already computed this can be specified to spare double-calculation
#     :return: tuple (groupwise_scales, logphi, rmsd)
#     """
#
#     def compute_scales_fun(variance, mean):
#         denominator = np.fmax(variance - mean, np.sqrt(np.nextafter(0, 1, dtype=variance.dtype)))
#         groupwise_scales = np.square(mean) / denominator
#         return groupwise_scales
#
#     return closedform_glm_scale(
#         X=X,
#         design_scale=design_scale,
#         constraints=constraints,
#         size_factors=size_factors,
#         groupwise_means=groupwise_means,
#         link_fn=link_fn,
#         compute_scales_fun=compute_scales_fun
#     )
