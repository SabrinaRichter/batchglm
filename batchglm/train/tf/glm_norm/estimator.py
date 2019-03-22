import logging
from typing import Union

import numpy as np
import tensorflow as tf

from .external import AbstractEstimator, EstimatorAll, ESTIMATOR_PARAMS, InputData, Model
from .external import data_utils
from .external import closedform_norm_glm_mean, closedform_norm_glm_logsd
from .external import SparseXArrayDataArray
from .estimator_graph import EstimatorGraph
from .model import ProcessModel
from .training_strategies import TrainingStrategies

logger = logging.getLogger(__name__)


class Estimator(EstimatorAll, AbstractEstimator, ProcessModel):
    """
    Estimator for Generalized Linear Models (GLMs) with normal distributed noise.
    Uses the identity function as linker function for loc and a log-linker function for scale.
    """

    def __init__(
            self,
            input_data: InputData,
            batch_size: int = 500,
            graph: tf.Graph = None,
            init_model: Model = None,
            init_a: Union[np.ndarray, str] = "AUTO",
            init_b: Union[np.ndarray, str] = "AUTO",
            quick_scale: bool = False,
            model: EstimatorGraph = None,
            provide_optimizers: dict = {
                "gd": True,
                "adam": True,
                "adagrad": True,
                "rmsprop": True,
                "nr": True,
                "nr_tr": True,
                "irls": True,
                "irls_gd": True,
                "irls_tr": True,
                "irls_gd_tr": True,
            },
            provide_batched: bool = False,
            extended_summary=False,
            dtype="float64"
    ):
        """
        Performs initialisation and creates a new estimator.

        :param input_data: InputData
            The input data
        :param batch_size: int
            Size of mini-batches used.
        :param graph: (optional) tf.Graph
        :param init_model: (optional)
            If provided, this model will be used to initialize this Estimator.
        :param init_a: (Optional)
            Low-level initial values for a. Can be:

            - str:
                * "auto": automatically choose best initialization
                * "all zero": initialize with zeros
                * "random": initialize with random values
                * "standard": initialize intercept with observed mean
                * "init_model": initialize with another model (see `ìnit_model` parameter)
                * "closed_form": try to initialize with closed form
            - np.ndarray: direct initialization of 'a'
        :param init_b: (Optional)
            Low-level initial values for b. Can be:

            - str:
                * "auto": automatically choose best initialization
                * "random": initialize with random values
                * "standard": initialize with zeros
                * "init_model": initialize with another model (see `ìnit_model` parameter)
                * "closed_form": try to initialize with closed form
            - np.ndarray: direct initialization of 'b'
        :param quick_scale: bool
            Whether `scale` will be fitted faster and maybe less accurate.
            Useful in scenarios where fitting the exact `scale` is not absolutely necessary.
        :param model: EstimatorGraph
            EstimatorGraph to use. Basically for debugging.
        :param provide_optimizers:

            E.g.    {"gd": False, "adam": False, "adagrad": False, "rmsprop": False,
                    "nr": False, "nr_tr": True,
                    "irls": False, "irls_gd": False, "irls_tr": False, "irls_gd_tr": False}
        :param provide_batched: bool
            Whether mini-batched optimizers should be provided.
        :param extended_summary: Include detailed information in the summaries.
            Will increase runtime of summary writer, use only for debugging.
        :param dtype: Precision used in tensorflow.
        """
        self.TrainingStrategies = TrainingStrategies

        self._input_data = input_data
        self._train_loc = True
        self._train_scale = not quick_scale

        (init_a, init_b) = self.init_par(
            input_data=input_data,
            init_a=init_a,
            init_b=init_b,
            init_model=init_model
        )
        init_a = init_a.astype(dtype)
        init_b = init_b.astype(dtype)

        EstimatorAll.__init__(
            self=self,
            input_data=input_data,
            batch_size=batch_size,
            graph=graph,
            init_a=init_a,
            init_b=init_b,
            model=model,
            provide_optimizers=provide_optimizers,
            provide_batched=provide_batched,
            extended_summary=extended_summary,
            noise_model="norm",
            dtype=dtype
        )

    @classmethod
    def param_shapes(cls) -> dict:
        return ESTIMATOR_PARAMS

    def init_par(
            self,
            input_data,
            init_a,
            init_b,
            init_model
    ):
        r"""
        standard:
        Only initialise intercept and keep other coefficients as zero.

        closed-form:
        Initialize with Maximum Likelihood / Maximum of Momentum estimators

        Idea:
        $$
            \theta &= f(x) \\
            \Rightarrow f^{-1}(\theta) &= x \\
                &= (D \cdot D^{+}) \cdot x \\
                &= D \cdot (D^{+} \cdot x) \\
                &= D \cdot x' = f^{-1}(\theta)
        $$
        """

        size_factors_init = input_data.size_factors
        if size_factors_init is not None:
            size_factors_init = np.expand_dims(size_factors_init, axis=1)
            size_factors_init = np.broadcast_to(
                array=size_factors_init,
                shape=[input_data.num_observations, input_data.num_features]
            )

        if init_model is None:
            groupwise_means = None
            init_a_str = None
            if isinstance(init_a, str):
                init_a_str = init_a.lower()
                # Chose option if auto was chosen
                if init_a.lower() == "auto":
                    init_a = "closed_form"

                if init_a.lower() == "closed_form":
                    groupwise_means, init_a, rmsd_a = closedform_norm_glm_mean(
                        X=input_data.X,
                        design_loc=input_data.design_loc,
                        constraints_loc=input_data.constraints_loc.values,
                        size_factors=size_factors_init,
                        link_fn=lambda mean: self.np_clip_param(mean, "mean")
                    )

                    # train mean, if the closed-form solution is inaccurate
                    self._train_loc = not (np.all(rmsd_a == 0) or rmsd_a.size == 0)

                    if input_data.size_factors is not None:
                        if np.any(input_data.size_factors != 1):
                            self._train_loc = True

                    logger.debug("Using closed-form MLE initialization for mean")
                    logger.debug("Should train mean: %s", self._train_loc)
                elif init_a.lower() == "standard":
                    if isinstance(input_data.X, SparseXArrayDataArray):
                        overall_means = input_data.X.mean(dim="observations")
                    else:
                        overall_means = input_data.X.mean(dim="observations").values  # directly calculate the mean
                    overall_means = self.np_clip_param(overall_means, "mean")

                    init_a = np.zeros([input_data.num_loc_params, input_data.num_features])
                    init_a[0, :] = overall_means
                    self._train_loc = True

                    logger.debug("Using standard initialization for mean")
                    logger.debug("Should train mean: %s", self._train_loc)
                elif init_a.lower() == "all_zero":
                    init_a = np.zeros([input_data.num_loc_params, input_data.num_features])
                    self._train_loc = True

                    logger.debug("Using all_zero initialization for mean")
                    logger.debug("Should train mean: %s", self._train_loc)
                else:
                    raise ValueError("init_a string %s not recognized" % init_a)

            if isinstance(init_b, str):
                if init_b.lower() == "auto":
                    init_b = "standard"

                if init_b.lower() == "closed_form" or init_b.lower() == "standard":
                    #try:
                    # Check whether it is necessary to recompute group-wise means.
                    dmats_unequal = False
                    if input_data.design_loc.shape[1] == input_data.design_scale.shape[1]:
                        if np.any(input_data.design_loc.values != input_data.design_scale.values):
                            dmats_unequal = True

                    inits_unequal = False
                    if init_a_str is not None:
                        if init_a_str != init_b:
                            inits_unequal = True

                    if inits_unequal or dmats_unequal:
                        groupwise_means = None
                        init_b = "standard"  # remove once have code to deal with this.

                    # Watch out: init_mean is full obs x features matrix and is very large in many cases.
                    if inits_unequal or dmats_unequal:
                        if isinstance(input_data.X, SparseXArrayDataArray):
                            init_mean = np.matmul(
                                    input_data.design_loc.values,
                                    np.matmul(input_data.constraints_loc.values, init_a)
                            )
                        else:
                            init_a_xr = data_utils.xarray_from_data(init_a, dims=("loc_params", "features"))
                            init_a_xr.coords["loc_params"] = input_data.constraints_loc.coords["loc_params"].values
                            init_mean = input_data.design_loc.dot(input_data.constraints_loc.dot(init_a_xr))

                        if size_factors_init is not None:
                            init_mean = init_mean / size_factors_init
                    else:
                        init_mean = None

                    if init_b.lower() == "closed_form":
                        groupwise_scales, init_b, rmsd_b = closedform_norm_glm_logsd(
                            X=input_data.X,
                            mean=init_mean,
                            design_scale=input_data.design_scale,
                            constraints=input_data.constraints_scale.values,
                            size_factors=size_factors_init,
                            groupwise_means=groupwise_means,
                            link_fn=lambda sd: np.log(self.np_clip_param(sd, "sd"))
                        )

                        logger.debug("Using closed-form MME initialization for standard deviation")
                        logger.debug("Should train sd: %s", self._train_scale)
                    elif init_b.lower() == "standard":
                        groupwise_scales, init_b_intercept, rmsd_b = closedform_norm_glm_logsd(
                            X=input_data.X,
                            mean=init_mean,
                            design_scale=input_data.design_scale[:, [0]],
                            constraints=input_data.constraints_scale[[0], [0]].values,
                            size_factors=size_factors_init,
                            groupwise_means=None,
                            link_fn=lambda sd: np.log(self.np_clip_param(sd, "sd"))
                        )
                        init_b = np.zeros([input_data.num_scale_params, input_data.X.shape[1]])
                        init_b[0, :] = init_b_intercept

                        logger.debug("Using closed-form MME initialization for standard deviation")
                        logger.debug("Should train sd: %s", self._train_scale)
                    #except np.linalg.LinAlgError:
                    #    logger.warning("Closed form initialization failed!")
                elif init_b.lower() == "all_zero":
                    init_b = np.zeros([input_data.num_scale_params, input_data.X.shape[1]])

                    logger.debug("Using standard initialization for standard deviation")
                    logger.debug("Should train sd: %s", self._train_scale)
                else:
                    raise ValueError("init_b string %s not recognized" % init_b)
        else:
            # Locations model:
            if isinstance(init_a, str) and (init_a.lower() == "auto" or init_a.lower() == "init_model"):
                my_loc_names = set(input_data.loc_names.values)
                my_loc_names = my_loc_names.intersection(set(init_model.input_data.loc_names.values))

                init_loc = np.zeros([input_data.num_loc_params, input_data.num_features])
                for parm in my_loc_names:
                    init_idx = np.where(init_model.input_data.loc_names == parm)[0]
                    my_idx = np.where(input_data.loc_names == parm)[0]
                    init_loc[my_idx] = init_model.a_var[init_idx]

                init_a = init_loc
                logger.debug("Using initialization based on input model for mean")

            # Scale model:
            if isinstance(init_b, str) and (init_b.lower() == "auto" or init_b.lower() == "init_model"):
                my_scale_names = set(input_data.scale_names.values)
                my_scale_names = my_scale_names.intersection(init_model.input_data.scale_names.values)

                init_scale = np.zeros([input_data.num_scale_params, input_data.num_features])
                for parm in my_scale_names:
                    init_idx = np.where(init_model.input_data.scale_names == parm)[0]
                    my_idx = np.where(input_data.scale_names == parm)[0]
                    init_scale[my_idx] = init_model.b_var[init_idx]

                init_b = init_scale
                logger.debug("Using initialization based on input model for dispersion")

        return init_a, init_b

    @property
    def input_data(self) -> InputData:
        return self._input_data
