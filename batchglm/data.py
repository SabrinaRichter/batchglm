from typing import Union, Dict, Tuple, List

import os
import tempfile
import zipfile as zf
import logging

import patsy
import pandas as pd
import numpy as np
import scipy.sparse
import xarray as xr
import dask
import dask.array

from .external import SparseXArrayDataArray, SparseXArrayDataSet

try:
    import anndata
except ImportError:
    anndata = None

logger = logging.getLogger(__name__)


def _sparse_to_xarray(data, dims):
    num_observations, num_features = data.shape

    def fetch_X(idx):
        idx = np.asarray(idx).reshape(-1)
        retval = data[idx].toarray()

        if idx.size == 1:
            retval = np.squeeze(retval, axis=0)

        return retval.astype(np.float64)

    delayed_fetch = dask.delayed(fetch_X, pure=True)
    X = [
        dask.array.from_delayed(
            delayed_fetch(idx),
            shape=(num_features,),
            dtype=np.float64
        ) for idx in range(num_observations)
    ]

    X = data

    # currently broken:
    # X = data.X
    # X = dask.array.from_array(X, X.shape)
    #
    # X = xr.DataArray(X, dims=dims)

    return X


def xarray_from_data(
        data: Union[anndata.AnnData, anndata.base.Raw, xr.DataArray, xr.Dataset, np.ndarray, scipy.sparse.csr_matrix],
        dims: Union[Tuple, List] = ("observations", "features")
):
    """
    Parse any array-like object, xr.DataArray, xr.Dataset or anndata.Anndata and return a xarray containing
    the observations.
    
    :param data: Array-like, xr.DataArray, xr.Dataset or anndata.Anndata object containing observations
    :param dims: tuple or list with two strings. Specifies the names of the xarray dimensions.
    :return: xr.DataArray of shape `dims`
    """
    if anndata is not None and (isinstance(data, anndata.AnnData) or isinstance(data, anndata.base.Raw)):
        # Anndata.raw does not have obs_names.
        if isinstance(data, anndata.AnnData):
            obs_names = np.asarray(data.obs_names)
        else:
            obs_names = ["obs_" + str(i) for i in range(data.X.shape[0])]

        if scipy.sparse.issparse(data.X):
            # X = _sparse_to_xarray(data.X, dims=dims)
            # X.coords[dims[0]] = np.asarray(data.obs_names)
            # X.coords[dims[1]] = np.asarray(data.var_names)
            X = SparseXArrayDataSet(
                X=data.X,
                obs_names=np.asarray(obs_names),
                feature_names=np.asarray(data.var_names),
                dims=dims
            )
        else:
            X = xr.DataArray(
                data.X,
                dims=dims,
                coords={
                    dims[0]: np.asarray(obs_names),
                    dims[1]: np.asarray(data.var_names),
                }
            )
    elif isinstance(data, xr.Dataset):
        X: xr.DataArray = data["X"]
    elif isinstance(data, xr.DataArray):
        X = data
    elif isinstance(data, SparseXArrayDataArray):
        X = data
    elif isinstance(data, SparseXArrayDataSet):
        X = data
    elif scipy.sparse.issparse(data):
        # X = _sparse_to_xarray(data, dims=dims)
        # X.coords[dims[0]] = np.asarray(data.obs_names)
        # X.coords[dims[1]] = np.asarray(data.var_names)
        X = SparseXArrayDataSet(
            X=data,
            obs_names=None,
            feature_names=None,
            dims=dims
        )
    elif isinstance(data, np.ndarray):
        X = xr.DataArray(data, dims=dims)
    else:
        raise ValueError("batchglm data parsing: data format %s not recognized" % type(data))

    return X


def design_matrix(
        sample_description: pd.DataFrame,
        formula: str,
        as_categorical: Union[bool, list] = True,
        return_type: str = "matrix",
) -> Union[patsy.design_info.DesignMatrix, xr.Dataset, pd.DataFrame]:
    """
    Create a design matrix from some sample description
    
    :param sample_description: pandas.DataFrame of length "num_observations" containing explanatory variables as columns
    :param formula: model formula as string, describing the relations of the explanatory variables.
    
        E.g. '~ 1 + batch + confounder'
    :param as_categorical: boolean or list of booleans corresponding to the columns in 'sample_description'
        
        If True, all values in 'sample_description' will be treated as categorical values.
        
        If list of booleans, each column will be changed to categorical if the corresponding value in 'as_categorical'
        is True.
        
        Set to false, if columns should not be changed.
    :param return_type: type of the returned value.

        - "matrix": return plain patsy.design_info.DesignMatrix object
        - "dataframe": return pd.DataFrame with observations as rows and params as columns
        - "xarray": return xr.Dataset with design matrix as ds["design"] and the sample description embedded as
            one variable per column
    :return: a model design matrix
    """
    sample_description: pd.DataFrame = sample_description.copy()

    if type(as_categorical) is not bool or as_categorical:
        if type(as_categorical) is bool and as_categorical:
            as_categorical = np.repeat(True, sample_description.columns.size)

        for to_cat, col in zip(as_categorical, sample_description):
            if to_cat:
                sample_description[col] = sample_description[col].astype("category")

    dmat = patsy.highlevel.dmatrix(formula, sample_description)

    if return_type == "dataframe":
        df = pd.DataFrame(dmat, columns=dmat.design_info.column_names)
        df = pd.concat([df, sample_description], axis=1)
        df.set_index(list(sample_description.columns), inplace=True)

        return df
    elif return_type == "xarray":
        ar = xr.DataArray(dmat, dims=("observations", "design_params"))
        ar.coords["design_params"] = dmat.design_info.column_names

        ds = xr.Dataset({
            "design": ar,
        })

        for col in sample_description:
            ds[col] = (("observations",), sample_description[col])

        return ds
    else:
        return dmat


def sample_description_from_xarray(
        dataset: xr.Dataset,
        dim: str,
):
    """
        Create a design matrix from a given xarray.Dataset and model formula.

        :param dataset: xarray.Dataset containing explanatory variables.
        :param dim: name of the dimension for which the design matrix should be created.

            The design matrix will be of shape (dim, "design_params").
        :return: pd.DataFrame
        """

    explanatory_vars = [key for key, val in dataset.variables.items() if val.dims == (dim,)]

    if len(explanatory_vars) > 0:
        sample_description = dataset[explanatory_vars].to_dataframe()
    else:
        sample_description = pd.DataFrame({"intercept": range(dataset.dims[dim])})

    return sample_description


def design_matrix_from_xarray(
        dataset: xr.Dataset,
        dim: str,
        formula=None,
        formula_key="formula",
        as_categorical=True,
        return_type="matrix",
):
    """
    Create a design matrix from a given xarray.Dataset and model formula.
    
    The formula will be chosen by the following order:
        1) from the parameter 'formula'
        2) from dataset[formula_key]

    The resulting design matrix as well as the formula and explanatory variables will be stored at the corresponding
    '\*_key' keys in the returned dataset.

    :param dim: name of the dimension for which the design matrix should be created.

        The design matrix will be of shape (dim, "design_params").
    :param dataset: xarray.Dataset containing explanatory variables.
    :param formula: model formula as string, describing the relations of the explanatory variables.
        If None, the formula is assumed to be stored inside 'dataset' as attribute
    
        E.g. '~ 1 + batch + condition'
    :param formula_key: index of the formula attribute inside 'dataset'.
        Will store the formula as `dataset.attrs[formula_key]` inside the dataset
    :param as_categorical: boolean or list of booleans corresponding to the columns in 'sample_description'
        
        If True, all values in 'sample_description' will be treated as categorical values.
        
        If list of booleans, each column will be changed to categorical if the corresponding value in 'as_categorical'
        is True.
        
        Set to false, if columns should not be changed.
    :param return_type: type of the returned data; see design_matrix() for details
    """
    if formula is None:
        formula = dataset.attrs.get(formula_key)
    if formula is None:
        raise ValueError("formula could not be found")

    sample_description = sample_description_from_xarray(dataset=dataset, dim=dim)

    dmat = design_matrix(
        sample_description=sample_description,
        formula=formula,
        as_categorical=as_categorical,
        return_type=return_type
    )

    return dmat


def sample_description_from_anndata(dataset: anndata.AnnData):
    """
    Create a design matrix from a given xarray.Dataset and model formula.

    :param dataset: anndata.AnnData containing explanatory variables.

    :return pd.DataFrame
    """

    return dataset.obs


def design_matrix_from_anndata(
        dataset: anndata.AnnData,
        formula=None,
        formula_key="formula",
        as_categorical=True,
        return_type="matrix",
):
    r"""
    Create a design matrix from a given xarray.Dataset and model formula.

    The formula will be chosen by the following order:
        1) from the parameter 'formula'
        2) from dataset.uns[formula_key]

    The resulting design matrix as well as the formula and explanatory variables will be stored at the corresponding
    '\*_key' keys in the returned dataset.

    :param dataset: anndata.AnnData containing explanatory variables.
    :param formula: model formula as string, describing the relations of the explanatory variables.
        If None, the formula is assumed to be stored inside 'dataset' as attribute

        E.g. '~ 1 + batch + condition'
    :param formula_key: index of the formula attribute inside 'dataset'.
        Will store the formula as `dataset.uns[formula_key]` inside the dataset
    :param as_categorical: boolean or list of booleans corresponding to the columns in 'sample_description'

        If True, all values in 'sample_description' will be treated as categorical values.

        If list of booleans, each column will be changed to categorical if the corresponding value in 'as_categorical'
        is True.

        Set to false, if columns should not be changed.
    :param return_type: type of the returned data; see design_matrix() for details
    """
    if formula is None:
        formula = dataset.uns.get(formula_key)
    if formula is None:
        # could not find formula; try to construct it from explanatory variables
        raise ValueError("formula could not be found")

    sample_description = sample_description_from_anndata(dataset=dataset)

    dmat = design_matrix(
        sample_description=sample_description,
        formula=formula,
        as_categorical=as_categorical,
        return_type=return_type
    )

    return dmat


def load_mtx_to_adata(path, cache=True):
    """
    Loads mtx file, genes and barcodes from a given directory into an `anndata.AnnData` object

    :param path: the folder containing the files
    :param cache: Should a cache file be used for the AnnData object?

        See `scanpy.api.read` for details.
    :return: `anndata.AnnData` object
    """
    import scanpy.api as sc

    adata = sc.read(os.path.join(path, "matrix.mtx"), cache=cache).T

    files = os.listdir(os.path.join(path))
    for file in files:
        if file.startswith("genes"):
            delim = ","
            if file.endswith("tsv"):
                delim = "\t"

            fpath = os.path.join(path, file)
            logger.info("Reading %s as gene annotation...", fpath)
            tbl = pd.read_csv(fpath, header=None, sep=delim)
            tbl.columns = np.vectorize(lambda x: "col_%d" % x)(tbl.columns)

            adata.var = tbl
            # ad.var_names = tbl[1]
        elif file.startswith("barcodes"):
            delim = ","
            if file.endswith("tsv"):
                delim = "\t"

            fpath = os.path.join(path, file)
            logger.info("Reading %s as barcode file...", fpath)
            tbl = pd.read_csv(fpath, header=None, sep=delim)
            tbl.columns = np.vectorize(lambda x: "col_%d" % x)(tbl.columns)

            adata.obs = tbl
            # ad.obs_names = tbl[0]
    # ad.var_names_make_unique()
    adata.var.columns = adata.var.columns.astype(str)
    adata.obs.columns = adata.obs.columns.astype(str)

    return adata


def load_mtx_to_xarray(path):
    """
    Loads mtx file, genes and barcodes from a given directory into an `xarray.DataArray` object

    :param path: the folder containing the files
    :return: `xarray.DataArray` object
    """
    import scanpy.api as sc

    matrix = sc.read(os.path.join(path, "matrix.mtx"), cache=False).X.toarray()

    # retval = xr.Dataset({
    #     "X": (["observations", "features"], np.transpose(matrix)),
    # })

    retval = xr.DataArray(np.transpose(matrix), dims=("observations", "features"))

    files = os.listdir(os.path.join(path))
    for file in files:
        if file.startswith("genes"):
            delim = ","
            if file.endswith("tsv"):
                delim = "\t"

            fpath = os.path.join(path, file)
            logger.info("Reading %s as gene annotation...", fpath)
            tbl = pd.read_csv(fpath, header=None, sep=delim)
            # retval["var"] = (["var_annotations", "features"], np.transpose(tbl))
            for col_id in tbl:
                retval.coords["gene_annot%d" % col_id] = ("features", tbl[col_id])
        elif file.startswith("barcodes"):
            delim = ","
            if file.endswith("tsv"):
                delim = "\t"

            fpath = os.path.join(path, file)
            logger.info("Reading %s as barcode file...", fpath)
            tbl = pd.read_csv(fpath, header=None, sep=delim)
            # retval["obs"] = (["obs_annotations", "observations"], np.transpose(tbl))
            for col_id in tbl:
                retval.coords["sample_annot%d" % col_id] = ("observations", tbl[col_id])
    return retval


def load_recursive_mtx(dir_or_zipfile, target_format="xarray", cache=True) -> Dict[str, xr.DataArray]:
    """
    Loads recursively all `mtx` structures inside a given directory or zip file

    :param dir_or_zipfile: directory or zip file which will be traversed
    :param target_format: format to read into. Either "xarray" or "adata"
    :param cache: option passed to `load_mtx_to_adata` when `target_format == "adata" or "anndata"`
    :return: Dict[str, xr.DataArray] containing {"path" : data}
    """
    dir_or_zipfile = os.path.expanduser(dir_or_zipfile)
    if dir_or_zipfile.endswith(".zip"):
        path = tempfile.mkdtemp()
        zip_ref = zf.ZipFile(dir_or_zipfile)
        zip_ref.extractall(path)
        zip_ref.close()
    else:
        path = dir_or_zipfile

    adatas = {}
    for root, dirs, files in os.walk(path):
        for file in files:
            if file == "matrix.mtx":
                if target_format.lower() == "xarray":
                    logger.info("Reading %s as xarray...", root)
                    ad = load_mtx_to_xarray(root)
                elif target_format.lower() == "adata" or target_format.lower() == "anndata":
                    logger.info("Reading %s as AnnData...", root)
                    ad = load_mtx_to_adata(root, cache=cache)
                else:
                    raise RuntimeError("Unknown target format %s" % target_format)

                adatas[root[len(path) + 1:]] = ad

    return adatas


def build_equality_constraints(
        sample_description: pd.DataFrame,
        formula: str,
        constraints: List[str],
        dims: list,
        as_categorical: Union[bool, list] = True
):
    """
    Create a design matrix from some sample description and a constraint matrix
    based on factor encoding of constrained parameter sets.

    :param sample_description: pandas.DataFrame of length "num_observations" containing explanatory variables as columns
    :param formula: model formula as string, describing the relations of the explanatory variables.

        E.g. '~ 1 + batch + confounder'
    :param constraints: List of constraints as strings, e.g. "x1 + x5 = 0".

        E.g. 'batch'
    :param as_categorical: boolean or list of booleans corresponding to the columns in 'sample_description'

        If True, all values in 'sample_description' will be treated as categorical values.

        If list of booleans, each column will be changed to categorical if the corresponding value in 'as_categorical'
        is True.

        Set to false, if columns should not be changed.
    :return: a model design matrix
    """
    dmat = design_matrix(
        sample_description=sample_description,
        formula=formula,
        as_categorical=as_categorical,
        return_type="xarray"
    )
    # Parse list of factors to be constrained to list of
    # string encoded explicit constraint equations.
    constraint_ls = ["+".join(patsy.highlevel.dmatrix("~1-1+"+x, sample_description))+"=0"
                     for x in constraints]
    logger.debug("constraints enforced are: "+",".join(constraint_ls))
    constraint_mat = build_equality_constraints_string(
        dmat=dmat,
        constraints=constraints_ls,
        dims=dims
    )

    return dmat, constraint_mat


def build_equality_constraints_string(
        dmat: Union[xr.DataArray, xr.Dataset],
        constraints: List[str],
        dims: list
):
    r"""
    Parser for string encoded equality constraints.

    :param dmat: Design matrix.
    :param constraints: List of constraints as strings.

        E.g. ["batch1 + batch2 + batch3 = 0"]
    :param dims: ["design_loc_params", "loc_params"] or ["design_scale_params", "scale_params"]
        Define dimension names of xarray.
    :return: a constraint matrix
    """
    if isinstance(dmat, xr.Dataset):
        dmat = dmat.data_vars['design']
    n_par_all = dmat.values.shape[1]
    n_par_free = n_par_all - len(constraints)

    di = patsy.DesignInfo(dmat.coords["design_params"].values)
    constraint_ls = [di.linear_constraint(x).coefs[0] for x in constraints]
    idx_constr = np.asarray([np.where(x == 1)[0][0] for x in constraint_ls])
    idx_depending = [np.where(x == 1)[0][1:] for x in constraint_ls]
    idx_unconstr = np.asarray(list(
        set(np.asarray(range(n_par_all))) - set(idx_constr)
    ))

    dmat_var = xr.DataArray(
        dims=[dmat.dims[0], "params"],
        data=dmat[:,idx_unconstr],
        coords={dmat.dims[0]: dmat.coords["observations"].values,
                "params": dmat.coords["design_params"].values[idx_unconstr]}
    )

    constraint_mat = np.zeros([n_par_all, n_par_free])
    for i in range(n_par_all):
        if i in idx_constr:
            idx_dep_i = idx_depending[np.where(idx_constr == i)[0][0]]
            idx_dep_i = np.asarray([np.where(idx_unconstr == x)[0] for x in idx_dep_i])
            constraint_mat[i, :] = 0
            constraint_mat[i, idx_dep_i] = -1
        else:
            idx_unconstr_i = np.where(idx_unconstr == i)
            constraint_mat[i, :] = 0
            constraint_mat[i, idx_unconstr_i] = 1

    constraints_ar = parse_constraints(
        dmat=dmat,
        constraints=constraint_mat,
        dims=dims
    )

    # Test reduced design matrix for full rank before returning constraints:
    if np.linalg.matrix_rank(dmat_var) != np.linalg.matrix_rank(dmat_var.T):
        logger.warning("constrained design matrix is not full rank")

    return constraints_ar


def parse_constraints(
        dmat: Union[xr.DataArray, xr.Dataset],
        constraints: np.ndarray,
        dims: list
):
    r"""
    Parse constraint matrix into xarray.

    :param dmat: Design matrix.
    :param constraints: a constraint matrix
    :return: constraint matrix in xarray format
    """
    if isinstance(dmat, xr.Dataset):
        dmat = dmat.data_vars['design']

    constraints_ar = xr.DataArray(
        dims=dims,
        data=constraints,
        coords={dims[0]: dmat.coords["design_params"].values,
                dims[1]: ["var_"+str(x) for x in range(constraints.shape[1])]}
    )
    return constraints_ar


class ChDir:
    """
    Context manager to temporarily change the working directory
    """

    def __init__(self, path):
        self.cwd = os.getcwd()
        self.other_wd = path

    def __enter__(self):
        os.chdir(self.other_wd)

    def __exit__(self, *args):
        os.chdir(self.cwd)
