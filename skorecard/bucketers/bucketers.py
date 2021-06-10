import warnings
import numpy as np
import pandas as pd

from sklearn.cluster import AgglomerativeClustering
from sklearn.tree import DecisionTreeClassifier
from sklearn.tree import _tree

from typing import Union, List, Dict
from skorecard.bucketers.base_bucketer import BaseBucketer
from skorecard.bucket_mapping import BucketMapping
from skorecard.features_bucket_mapping import FeaturesBucketMapping
from skorecard.utils import NotInstalledError, NotPreBucketedError
from skorecard.utils.exceptions import ApproximationWarning
from skorecard.reporting import build_bucket_table


try:
    from optbinning import OptimalBinning
except ModuleNotFoundError:
    OptimalBinning = NotInstalledError("optbinning")


class OptimalBucketer(BaseBucketer):
    """
    The `OptimalBucketer` transformer uses the [optbinning](http://gnpalencia.org/optbinning) package to find optimal buckets.

    Support: ![badge](https://img.shields.io/badge/numerical-true-green) ![badge](https://img.shields.io/badge/categorical-true-green) ![badge](https://img.shields.io/badge/supervised-true-green)

    This bucketer basically wraps optbinning.OptimalBinning to be consistent with skorecard.
    Requires a feature to be pre-bucketed to max 100 buckets.
    Optbinning uses a constrained programming solver to merge buckets,
    taking into account the following constraints 1) monotonicity in bad rate, 2) at least 5% of records per bin.

    Example:

    ```python
    from skorecard import datasets
    from skorecard.bucketers import OptimalBucketer

    X, y = datasets.load_uci_credit_card(return_X_y=True)
    bucketer = OptimalBucketer(variables = ['LIMIT_BAL'])
    bucketer.fit_transform(X, y)
    ```
    """  # noqa

    def __init__(
        self,
        variables=[],
        specials={},
        variables_type="numerical",
        max_n_bins=10,
        missing_treatment="separate",
        min_bin_size=0.05,
        cat_cutoff=None,
        time_limit=25,
        remainder="passthrough",
        **kwargs,
    ) -> None:
        """Initialize Optimal Bucketer.

        Args:
            variables: List of variables to bucket.
            specials: (nested) dictionary of special values that require their own binning.
                The dictionary has the following format:
                 {"<column name>" : {"name of special bucket" : <list with 1 or more values>}}
                For every feature that needs a special value, a dictionary must be passed as value.
                This dictionary contains a name of a bucket (key) and an array of unique values that should be put
                in that bucket.
                When special values are passed, they are not considered in the fitting procedure.
            variables_type: Type of the variables
            missing_treatment: Defines how we treat the missing values present in the data.
                If a string, it must be in ['separate', 'most_risky', 'most_frequent']
                    separate: Missing values get put in a separate 'Other' bucket: `-1`
                    most_risky: not yet implemented
                    most_frequent: not yet implemented
                If a dict, it must be of the following format:
                    {"<column name>": <bucket_number>}
                    This bucket number is where we will put the missing values.
            min_bin_size: Minimum fraction of observations in a bucket. Passed to optbinning.OptimalBinning.
            max_n_bins: Maximum numbers of bins to return. Passed to optbinning.OptimalBinning.
            cat_cutoff: Threshold ratio (None, or >0 and <=1) below which categories are grouped
                together in a bucket 'other'. Passed to optbinning.OptimalBinning.
            time_limit: Time limit in seconds to find an optimal solution. Passed to optbinning.OptimalBinning.
            remainder: How we want the non-specified columns to be transformed. It must be in ["passthrough", "drop"].
                passthrough (Default): all columns that were not specified in "variables" will be passed through.
                drop: all remaining columns that were not specified in "variables" will be dropped.
            kwargs: Other parameters passed to optbinning.OptimalBinning. Passed to optbinning.OptimalBinning.
        """
        self._is_allowed_missing_treatment(missing_treatment)
        assert variables_type in ["numerical", "categorical"]
        assert remainder in ["passthrough", "drop"]

        self.variables = variables
        self.specials = specials
        self.variables_type = variables_type
        self.max_n_bins = max_n_bins
        self.missing_treatment = missing_treatment
        self.min_bin_size = min_bin_size
        self.cat_cutoff = cat_cutoff
        self.time_limit = time_limit
        self.remainder = remainder

        self.kwargs = kwargs

    def fit(self, X, y):
        """Fit X, y."""
        X = self._is_dataframe(X)
        self.variables = self._check_variables(X, self.variables)
        self._verify_specials_variables(self.specials, X.columns)

        if isinstance(y, pd.Series):
            y = y.values

        features_bucket_mapping_ = {}
        self.bucket_tables_ = {}
        self.binners = {}

        for feature in self.variables:

            if feature in self.specials.keys():
                special = self.specials[feature]
                X_flt, y_flt = self._filter_specials_for_fit(
                    X=X[feature], y=y, specials=special
                )
            else:
                X_flt, y_flt = X[feature], y
                special = {}
            if self.variables_type == "numerical":
                X_flt, y_flt = self._filter_na_for_fit(X=X_flt, y=y_flt)
                uniq_values = np.sort(np.unique(X_flt.values))
                if len(uniq_values) > 100:
                    raise NotPreBucketedError(
                        f"""
                        OptimalBucketer requires numerical feature '{feature}' to be pre-bucketed
                        to max 100 unique values (for performance reasons).
                        Currently there are {len(uniq_values)} unique values present.

                        Apply pre-binning, f.e. with skorecard.bucketers.DecisionTreeBucketer.
                        """
                    )
                user_splits = uniq_values
            else:
                X_flt, y_flt = self._filter_na_for_fit(X=X_flt, y=y_flt)
                user_splits = None

            binner = OptimalBinning(
                name=feature,
                dtype=self.variables_type,
                solver="cp",
                monotonic_trend="auto_asc_desc",
                # We want skorecard users to explicitly define pre-binning for numerical features
                # Setting the user_splits prevents OptimalBinning from doing pre-binning again.
                user_splits=user_splits,
                min_bin_size=self.min_bin_size,
                max_n_bins=self.max_n_bins,
                cat_cutoff=self.cat_cutoff,
                time_limit=self.time_limit,
                **self.kwargs,
            )
            self.binners[feature] = binner

            binner.fit(X_flt.values, y_flt)

            # Extract fitted boundaries
            if self.variables_type == "categorical":
                splits = {}
                for bucket_nr, values in enumerate(binner.splits):
                    for value in values:
                        splits[value] = bucket_nr
            else:
                splits = binner.splits

            # Deal with missing values
            if self.missing_treatment in ["separate", "most_frequent"]:
                missing_bucket = None
            if isinstance(self.missing_treatment, dict):
                missing_bucket = self.missing_treatment.get(feature)

            # Note that optbinning transform uses right=False
            # https://github.com/guillermo-navas-palencia/optbinning/blob/396b9bed97581094167c9eb4744c2fd1fb5c7408/optbinning/binning/transformations.py#L126-L132
            features_bucket_mapping_[feature] = BucketMapping(
                feature_name=feature,
                type=self.variables_type,
                map=splits,
                right=False,
                specials=special,
                missing_bucket=missing_bucket,
            )

            # Calculate the bucket table
            self.bucket_tables_[feature] = build_bucket_table(
                X,
                y,
                column=feature,
                bucket_mapping=features_bucket_mapping_.get(feature),
            )

            if self.missing_treatment == "most_frequent":
                most_frequent_row = (
                    self.bucket_tables_[feature]
                    .sort_values("Count", ascending=False)
                    .reset_index(drop=True)
                    .iloc[0]
                )
                if most_frequent_row["label"] != "Missing":
                    missing_bucket = int(most_frequent_row["bucket_id"])
                else:
                    # missings are already the most common bucket, pick the next one
                    missing_bucket = int(
                        self.bucket_tables_[feature]
                        .sort_values("Count", ascending=False)
                        .reset_index(drop=True)["bucket_id"][1]
                    )
                # Repeat above procedure now we know the bucket distribution
                features_bucket_mapping_[feature] = BucketMapping(
                    feature_name=feature,
                    type=self.variables_type,
                    map=splits,
                    right=False,
                    specials=special,
                    missing_bucket=missing_bucket,
                )

                # Recalculate the bucket table with the new bucket for missings
                self.bucket_tables_[feature] = build_bucket_table(
                    X,
                    y,
                    column=feature,
                    bucket_mapping=features_bucket_mapping_.get(feature),
                )

        self.features_bucket_mapping_ = FeaturesBucketMapping(features_bucket_mapping_)

        self._generate_summary(X, y)

        return self

    def transform(self, X):
        """Transform X."""
        return super().transform(X)


class EqualWidthBucketer(BaseBucketer):
    """
    The `EqualWidthBucketer` transformer creates equally spaced bins using [numpy.histogram](https://numpy.org/doc/stable/reference/generated/numpy.histogram.html)
    function.

    Support: ![badge](https://img.shields.io/badge/numerical-true-green) ![badge](https://img.shields.io/badge/categorical-false-red) ![badge](https://img.shields.io/badge/supervised-false-red)

    Example:

    ```python
    from skorecard import datasets
    from skorecard.bucketers import EqualWidthBucketer

    specials = {"LIMIT_BAL": {"=50000": [50000], "in [20001,30000]": [20000, 30000]}}

    X, y = datasets.load_uci_credit_card(return_X_y=True)
    bucketer = EqualWidthBucketer(n_bins = 10, variables = ['LIMIT_BAL'], specials=specials)
    bucketer.fit_transform(X)
    bucketer.fit_transform(X)['LIMIT_BAL'].value_counts()
    ```
    """  # noqa

    def __init__(
        self,
        n_bins=5,
        variables=[],
        specials={},
        missing_treatment="separate",
        remainder="passthrough",
    ):
        """Init the class.

        Args:
            n_bins (int): Number of bins to create.
            variables (list): The features to bucket. Uses all features if not defined.
            specials: (dict) of special values that require their own binning.
                The dictionary has the following format:
                 {"<column name>" : {"name of special bucket" : <list with 1 or more values>}}
                For every feature that needs a special value, a dictionary must be passed as value.
                This dictionary contains a name of a bucket (key) and an array of unique values that should be put
                in that bucket.
                When special values are defined, they are not considered in the fitting procedure.
            missing_treatment: Defines how we treat the missing values present in the data.
                If a string, it must be in ['separate', 'most_risky', 'most_frequent']
                    separate: Missing values get put in a separate 'Other' bucket: `-1`
                    most_risky: not yet implemented
                    most_frequent: not yet implemented
                If a dict, it must be of the following format:
                    {"<column name>": <bucket_number>}
                    This bucket number is where we will put the missing values.
            remainder: How we want the non-specified columns to be transformed. It must be in ["passthrough", "drop"].
                passthrough (Default): all columns that were not specified in "variables" will be passed through.
                drop: all remaining columns that were not specified in "variables" will be dropped.
        """
        assert isinstance(variables, list)
        assert isinstance(n_bins, int)
        assert n_bins >= 1
        assert remainder in ["passthrough", "drop"]
        self._is_allowed_missing_treatment(missing_treatment)

        self.missing_treatment = missing_treatment
        self.variables = variables
        self.n_bins = n_bins
        self.specials = specials
        self.remainder = remainder

    def fit(self, X, y=None):
        """Fit X, y."""
        X = self._is_dataframe(X)
        self.variables = self._check_variables(X, self.variables)
        self._verify_specials_variables(self.specials, X.columns)

        features_bucket_mapping_ = {}
        self.bucket_tables_ = {}

        for feature in self.variables:

            if feature in self.specials.keys():
                special = self.specials[feature]
                X_flt, y_flt = self._filter_specials_for_fit(
                    X=X[feature], y=y, specials=special
                )
            else:
                X_flt = X[feature]
                y_flt = y
                special = {}

            X_flt, y_flt = self._filter_na_for_fit(X=X_flt, y=y_flt)
            _, boundaries = np.histogram(X_flt.values, bins=self.n_bins)

            # np.histogram returns the min & max values of the fits
            # On transform, we use np.digitize, which means new data that is outside of this range
            # will be assigned to their own buckets.
            # To solve, we simply remove the min and max boundaries
            boundaries = boundaries[1:-1]

            if isinstance(boundaries, np.ndarray):
                boundaries = boundaries.tolist()

            # Deal with missing values
            if self.missing_treatment in ["separate", "most_frequent"]:
                missing_bucket = None
            if isinstance(self.missing_treatment, dict):
                missing_bucket = self.missing_treatment.get(feature)

            features_bucket_mapping_[feature] = BucketMapping(
                feature_name=feature,
                type="numerical",
                map=boundaries,
                right=True,
                specials=special,
                missing_bucket=missing_bucket,
            )

            # Calculate the bucket table
            self.bucket_tables_[feature] = build_bucket_table(
                X,
                y,
                column=feature,
                bucket_mapping=features_bucket_mapping_.get(feature),
            )

            if self.missing_treatment == "most_frequent":
                most_frequent_row = (
                    self.bucket_tables_[feature]
                    .sort_values("Count", ascending=False)
                    .reset_index(drop=True)
                    .iloc[0]
                )
                if most_frequent_row["label"] != "Missing":
                    missing_bucket = int(most_frequent_row["bucket_id"])
                else:
                    # missings are already the most common bucket, pick the next one
                    missing_bucket = int(
                        self.bucket_tables_[feature]
                        .sort_values("Count", ascending=False)
                        .reset_index(drop=True)["bucket_id"][1]
                    )
                # Repeat above procedure now we know the bucket distribution
                features_bucket_mapping_[feature] = BucketMapping(
                    feature_name=feature,
                    type="numerical",
                    map=boundaries,
                    right=True,
                    specials=special,
                    missing_bucket=missing_bucket,
                )

                # Recalculate the bucket table with the new bucket for missings
                self.bucket_tables_[feature] = build_bucket_table(
                    X,
                    y,
                    column=feature,
                    bucket_mapping=features_bucket_mapping_.get(feature),
                )

        self.features_bucket_mapping_ = FeaturesBucketMapping(features_bucket_mapping_)

        self._generate_summary(X, y)

        return self


class AgglomerativeClusteringBucketer(BaseBucketer):
    """
    The `AgglomerativeClusteringBucketer` transformer creates buckets using [sklearn.AgglomerativeClustering](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.AgglomerativeClustering.html).

    Support ![badge](https://img.shields.io/badge/numerical-true-green) ![badge](https://img.shields.io/badge/categorical-false-red) ![badge](https://img.shields.io/badge/supervised-false-red)

    Example:

    ```python
    from skorecard import datasets
    from skorecard.bucketers import AgglomerativeClusteringBucketer

    specials = {"LIMIT_BAL": {"=50000": [50000], "in [20001,30000]": [20000, 30000]}}

    X, y = datasets.load_uci_credit_card(return_X_y=True)
    bucketer = AgglomerativeClusteringBucketer(n_bins = 10, variables=['LIMIT_BAL'], specials=specials)
    bucketer.fit_transform(X)
    bucketer.fit_transform(X)['LIMIT_BAL'].value_counts()

    # You can also access the fitted AgglomerativeClustering per feature:
    bucketer.binners['LIMIT_BAL']
    ```
    """  # noqa

    def __init__(
        self,
        n_bins=5,
        variables=[],
        specials={},
        missing_treatment="separate",
        remainder="passthrough",
        **kwargs,
    ):
        """Init the class.

        Args:
            n_bins (int): Number of bins to create.
            variables (list): The features to bucket. Uses all features if not defined.
            specials: (dict) of special values that require their own binning.
                The dictionary has the following format:
                 {"<column name>" : {"name of special bucket" : <list with 1 or more values>}}
                For every feature that needs a special value, a dictionary must be passed as value.
                This dictionary contains a name of a bucket (key) and an array of unique values that should be put
                in that bucket.
                When special values are defined, they are not considered in the fitting procedure.
            missing_treatment: Defines how we treat the missing values present in the data.
                If a string, it must be in ['separate', 'most_risky', 'most_frequent']
                    separate: Missing values get put in a separate 'Other' bucket: `-1`
                    most_risky: not yet implemented
                    most_frequent: not yet implemented
                If a dict, it must be of the following format:
                    {"<column name>": <bucket_number>}
                    This bucket number is where we will put the missing values.
            remainder: How we want the non-specified columns to be transformed. It must be in ["passthrough", "drop"].
                passthrough (Default): all columns that were not specified in "variables" will be passed through.
                drop: all remaining columns that were not specified in "variables" will be dropped.
            kwargs: Other parameters passed to AgglomerativeBucketer
        """
        assert isinstance(variables, list)
        assert isinstance(n_bins, int)
        assert n_bins >= 1
        assert remainder in ["passthrough", "drop"]
        self._is_allowed_missing_treatment(missing_treatment)

        self.variables = variables
        self.n_bins = n_bins
        self.specials = specials
        self.missing_treatment = missing_treatment
        self.remainder = remainder
        self.kwargs = kwargs

    def fit(self, X, y=None):
        """Fit X, y."""
        X = self._is_dataframe(X)
        self.variables = self._check_variables(X, self.variables)
        self._verify_specials_variables(self.specials, X.columns)

        features_bucket_mapping_ = {}
        self.binners = {}
        self.bucket_tables_ = {}

        for feature in self.variables:
            ab = AgglomerativeClustering(n_clusters=self.n_bins, **self.kwargs)

            if feature in self.specials.keys():
                special = self.specials[feature]
                X_flt, y_flt = self._filter_specials_for_fit(
                    X=X[feature], y=y, specials=special
                )
            else:
                X_flt = X[feature]
                y_flt = y
                special = {}
            X_flt, y_flt = self._filter_na_for_fit(X=X_flt, y=y_flt)

            ab.fit(X_flt.values.reshape(-1, 1), y=None)
            self.binners[feature] = ab

            # Find the boundaries
            df = pd.DataFrame({"x": X_flt.values, "label": ab.labels_}).sort_values(
                by="x"
            )
            cluster_minimum_values = (
                df.groupby("label")["x"].min().sort_values().tolist()
            )
            cluster_maximum_values = (
                df.groupby("label")["x"].max().sort_values().tolist()
            )
            # take the mean of the upper boundary of a cluster and the lower boundary of the next cluster
            boundaries = [
                # Assures numbers are float and not np.float - necessary for serialization
                float(
                    np.mean([cluster_minimum_values[i + 1], cluster_maximum_values[i]])
                )
                for i in range(len(cluster_minimum_values) - 1)
            ]

            if isinstance(boundaries, np.ndarray):
                boundaries = boundaries.tolist()

            # Deal with missing values
            if self.missing_treatment in ["separate", "most_frequent"]:
                missing_bucket = None
            if isinstance(self.missing_treatment, dict):
                missing_bucket = self.missing_treatment.get(feature)

            features_bucket_mapping_[feature] = BucketMapping(
                feature_name=feature,
                type="numerical",
                map=boundaries,
                right=True,
                specials=special,
                missing_bucket=missing_bucket,
            )

            # Calculate the bucket table
            self.bucket_tables_[feature] = build_bucket_table(
                X,
                y,
                column=feature,
                bucket_mapping=features_bucket_mapping_.get(feature),
            )

            if self.missing_treatment == "most_frequent":
                most_frequent_row = (
                    self.bucket_tables_[feature]
                    .sort_values("Count", ascending=False)
                    .reset_index(drop=True)
                    .iloc[0]
                )
                if most_frequent_row["label"] != "Missing":
                    missing_bucket = int(most_frequent_row["bucket_id"])
                else:
                    # missings are already the most common bucket, pick the next one
                    missing_bucket = int(
                        self.bucket_tables_[feature]
                        .sort_values("Count", ascending=False)
                        .reset_index(drop=True)["bucket_id"][1]
                    )
                # Repeat above procedure now we know the bucket distribution
                features_bucket_mapping_[feature] = BucketMapping(
                    feature_name=feature,
                    type="numerical",
                    map=boundaries,
                    right=True,
                    specials=special,
                    missing_bucket=missing_bucket,
                )

                # Recalculate the bucket table with the new bucket for missings
                self.bucket_tables_[feature] = build_bucket_table(
                    X,
                    y,
                    column=feature,
                    bucket_mapping=features_bucket_mapping_.get(feature),
                )

        self.features_bucket_mapping_ = FeaturesBucketMapping(features_bucket_mapping_)

        self._generate_summary(X, y)

        return self


class EqualFrequencyBucketer(BaseBucketer):
    """
    The `EqualFrequencyBucketer` transformer creates buckets with equal number of elements.

    Support: ![badge](https://img.shields.io/badge/numerical-true-green) ![badge](https://img.shields.io/badge/categorical-false-red) ![badge](https://img.shields.io/badge/supervised-false-red)

    Example:

    ```python
    from skorecard import datasets
    from skorecard.bucketers import EqualFrequencyBucketer

    X, y = datasets.load_uci_credit_card(return_X_y=True)
    bucketer = EqualFrequencyBucketer(n_bins = 10, variables=['LIMIT_BAL'])
    bucketer.fit_transform(X)
    bucketer.fit_transform(X)['LIMIT_BAL'].value_counts()
    ```
    """  # noqa

    def __init__(
        self,
        n_bins=5,
        variables=[],
        specials={},
        missing_treatment="separate",
        remainder="passthrough",
    ):
        """Init the class.

        Args:
            n_bins (int): Number of bins to create.
            variables (list): The features to bucket. Uses all features if not defined.
            specials: (nested) dictionary of special values that require their own binning.
                The dictionary has the following format:
                 {"<column name>" : {"name of special bucket" : <list with 1 or more values>}}
                For every feature that needs a special value, a dictionary must be passed as value.
                This dictionary contains a name of a bucket (key) and an array of unique values that should be put
                in that bucket.
                When special values are defined, they are not considered in the fitting procedure.
            missing_treatment: Defines how we treat the missing values present in the data.
                If a string, it must be in ['separate', 'most_risky', 'most_frequent']
                    separate: Missing values get put in a separate 'Other' bucket: `-1`
                    most_risky: not yet implemented
                    most_frequent: not yet implemented
                If a dict, it must be of the following format:
                    {"<column name>": <bucket_number>}
                    This bucket number is where we will put the missing values..
            remainder: How we want the non-specified columns to be transformed. It must be in ["passthrough", "drop"].
                passthrough (Default): all columns that were not specified in "variables" will be passed through.
                drop: all remaining columns that were not specified in "variables" will be dropped.
        """
        assert isinstance(variables, list)
        assert isinstance(n_bins, int)
        assert n_bins >= 1
        assert remainder in ["passthrough", "drop"]
        self._is_allowed_missing_treatment(missing_treatment)

        self.variables = variables
        self.n_bins = n_bins
        self.specials = specials
        self.missing_treatment = missing_treatment
        self.remainder = remainder

    def fit(self, X, y=None):
        """Fit X, y.

        Uses pd.qcut()
        https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.qcut.html

        """
        X = self._is_dataframe(X)
        self.variables = self._check_variables(X, self.variables)
        self._verify_specials_variables(self.specials, X.columns)

        features_bucket_mapping_ = {}
        self.bucket_tables_ = {}

        for feature in self.variables:

            if feature in self.specials.keys():
                special = self.specials[feature]
                X_flt, y_flt = self._filter_specials_for_fit(
                    X=X[feature], y=y, specials=special
                )
            else:
                X_flt = X[feature]
                special = {}
            try:
                _, boundaries = pd.qcut(
                    X_flt, q=self.n_bins, retbins=True, duplicates="raise"
                )
            except ValueError:
                # If there are too many duplicate values (assume a lot of filled missings)
                # this crashes - the exception drops them.
                # This means that it will return approximate quantile bins
                _, boundaries = pd.qcut(
                    X_flt, q=self.n_bins, retbins=True, duplicates="drop"
                )
                warnings.warn(
                    ApproximationWarning(
                        "Approximated quantiles - too many unique values"
                    )
                )

            # pd.qcut returns the min & max values of the fits
            # On transform, we use np.digitize, which means new data that is outside of this range
            # will be assigned to their own buckets.
            # To solve, we simply remove the min and max boundaries
            boundaries = boundaries[1:-1]

            if isinstance(boundaries, np.ndarray):
                boundaries = boundaries.tolist()

            # Deal with missing values
            if self.missing_treatment in ["separate", "most_frequent"]:
                missing_bucket = None
            if isinstance(self.missing_treatment, dict):
                missing_bucket = self.missing_treatment.get(feature)

            features_bucket_mapping_[feature] = BucketMapping(
                feature_name=feature,
                type="numerical",
                map=boundaries,
                right=True,  # pd.qcut returns bins including right edge: (edge, edge]
                specials=special,
                missing_bucket=missing_bucket,
            )

            # Calculate the bucket table
            self.bucket_tables_[feature] = build_bucket_table(
                X,
                y,
                column=feature,
                bucket_mapping=features_bucket_mapping_.get(feature),
            )

            if self.missing_treatment == "most_frequent":
                most_frequent_row = (
                    self.bucket_tables_[feature]
                    .sort_values("Count", ascending=False)
                    .reset_index(drop=True)
                    .iloc[0]
                )
                if most_frequent_row["label"] != "Missing":
                    missing_bucket = int(most_frequent_row["bucket_id"])
                else:
                    # missings are already the most common bucket, pick the next one
                    missing_bucket = int(
                        self.bucket_tables_[feature]
                        .sort_values("Count", ascending=False)
                        .reset_index(drop=True)["bucket_id"][1]
                    )
                # Repeat above procedure now we know the bucket distribution
                features_bucket_mapping_[feature] = BucketMapping(
                    feature_name=feature,
                    type="numerical",
                    map=boundaries,
                    right=True,  # pd.qcut returns bins including right edge: (edge, edge]
                    specials=special,
                    missing_bucket=missing_bucket,
                )

                # Recalculate the bucket table with the new bucket for missings
                self.bucket_tables_[feature] = build_bucket_table(
                    X,
                    y,
                    column=feature,
                    bucket_mapping=features_bucket_mapping_.get(feature),
                )

        self.features_bucket_mapping_ = FeaturesBucketMapping(features_bucket_mapping_)

        self._generate_summary(X, y)

        return self


class DecisionTreeBucketer(BaseBucketer):
    """
    The `DecisionTreeBucketer` transformer creates buckets by training a decision tree.

    Support: ![badge](https://img.shields.io/badge/numerical-true-green) ![badge](https://img.shields.io/badge/categorical-false-red) ![badge](https://img.shields.io/badge/supervised-true-green)

    It uses [sklearn.tree.DecisionTreeClassifier](https://scikit-learn.org/stable/modules/generated/sklearn.tree.DecisionTreeClassifier.html)
    to find the splits.

    Example:

    ```python
    from skorecard import datasets
    from skorecard.bucketers import DecisionTreeBucketer
    X, y = datasets.load_uci_credit_card(return_X_y=True)

    # make sure that those cases
    specials = {
        "LIMIT_BAL":{
            "=50000":[50000],
            "in [20001,30000]":[20000,30000],
            }
    }

    dt_bucketer = DecisionTreeBucketer(variables=['LIMIT_BAL'], specials = specials)
    dt_bucketer.fit(X, y)

    dt_bucketer.fit_transform(X, y)['LIMIT_BAL'].value_counts()

    # You can also access the fitted DecisionTreeClassifier per feature:
    dt_bucketer.binners['LIMIT_BAL']
    ```
    """  # noqa

    def __init__(
        self,
        variables=[],
        specials={},
        max_n_bins=100,
        missing_treatment="separate",
        min_bin_size=0.05,
        random_state=42,
        remainder="passthrough",
        **kwargs,
    ) -> None:
        """Init the class.

        Args:
            variables (list): The features to bucket. Uses all features if not defined.
            specials (dict):  dictionary of special values that require their own binning.
                The dictionary has the following format:
                 {"<column name>" : {"name of special bucket" : <list with 1 or more values>}}
                For every feature that needs a special value, a dictionary must be passed as value.
                This dictionary contains a name of a bucket (key) and an array of unique values that should be put
                in that bucket.
                When special values are defined, they are not considered in the fitting procedure.
            min_bin_size (int): Minimum fraction of observations in a bucket. Passed directly to min_samples_leaf.
            max_n_bins (int): Maximum numbers of after the bucketing. Passed directly to max_leaf_nodes of the
                DecisionTreeClassifier.
                If specials are defined, max_leaf_nodes will be redefined to max_n_bins - (number of special bins).
                The DecisionTreeClassifier requires max_leaf_nodes>=2:
                therefore, max_n_bins  must always be >= (number of special bins + 2) if specials are defined,
                otherwise must be >=2.
            missing_treatment (str or dict): Defines how we treat the missing values present in the data.
                If a string, it must be in ['separate', 'most_risky', 'most_frequent']
                    separate: Missing values get put in a separate 'Other' bucket: `-1`
                    most_risky: not yet implemented
                    most_frequent: not yet implemented
                If a dict, it must be of the following format:
                    {"<column name>": <bucket_number>}
                    This bucket number is where we will put the missing values.
            random_state (int): The random state, Passed directly to DecisionTreeClassifier
            remainder (str): How we want the non-specified columns to be transformed. It must be in ["passthrough", "drop"].
                passthrough (Default): all columns that were not specified in "variables" will be passed through.
                drop: all remaining columns that were not specified in "variables" will be dropped.
            kwargs: Other parameters passed to DecisionTreeClassifier
        """  # noqa
        assert isinstance(variables, list)
        assert remainder in ["passthrough", "drop"]
        self._is_allowed_missing_treatment(missing_treatment)

        self.variables = variables
        self.specials = specials
        self.kwargs = kwargs
        self.max_n_bins = max_n_bins
        self.missing_treatment = missing_treatment
        self.min_bin_size = min_bin_size
        self.random_state = random_state
        self.remainder = remainder

    def fit(self, X, y):
        """Fit X, y."""
        X = self._is_dataframe(X)
        self.variables = self._check_variables(X, self.variables)
        self._verify_specials_variables(self.specials, X.columns)

        features_bucket_mapping_ = {}
        self.bucket_tables_ = {}
        self.binners = {}

        for feature in self.variables:

            n_special_bins = 0
            if feature in self.specials.keys():
                special = self.specials[feature]

                n_special_bins = len(special)

                if (self.max_n_bins - n_special_bins) <= 1:
                    raise ValueError(
                        f"max_n_bins must be at least = the number of special bins + 2: set a value "
                        f"max_n_bins>= {n_special_bins+2} (currently max_n_bins={self.max_n_bins})"
                    )

                X_flt, y_flt = self._filter_specials_for_fit(
                    X=X[feature], y=y, specials=special
                )
            else:
                X_flt = X[feature]
                y_flt = y
                special = {}

            X_flt, y_flt = self._filter_na_for_fit(X=X_flt, y=y_flt)
            # If the specials are excluded, make sure that the bin size is rescaled.
            frac_left = X_flt.shape[0] / X.shape[0]

            # in case everything is a special case, don't fit the tree.
            if frac_left > 0:

                min_bin_size = self.min_bin_size / frac_left

                if min_bin_size > 0.5:
                    min_bin_size = 0.5

                binner = DecisionTreeClassifier(
                    max_leaf_nodes=(self.max_n_bins - n_special_bins),
                    min_samples_leaf=min_bin_size,
                    random_state=self.random_state,
                    **self.kwargs,
                )
                self.binners[feature] = binner
                binner.fit(X_flt.values.reshape(-1, 1), y_flt)

                # Extract fitted boundaries
                splits = np.unique(
                    binner.tree_.threshold[binner.tree_.feature != _tree.TREE_UNDEFINED]
                )

            else:
                splits = []

            # Deal with missing values
            if self.missing_treatment in ["separate", "most_frequent"]:
                missing_bucket = None
            if isinstance(self.missing_treatment, dict):
                missing_bucket = self.missing_treatment.get(feature)

            features_bucket_mapping_[feature] = BucketMapping(
                feature_name=feature,
                type="numerical",
                map=splits,
                right=False,  # note this is not default!
                specials=special,
                missing_bucket=missing_bucket,
            )

            # Calculate the bucket table
            self.bucket_tables_[feature] = build_bucket_table(
                X,
                y,
                column=feature,
                bucket_mapping=features_bucket_mapping_.get(feature),
            )

            if self.missing_treatment == "most_frequent":
                most_frequent_row = (
                    self.bucket_tables_[feature]
                    .sort_values("Count", ascending=False)
                    .reset_index(drop=True)
                    .iloc[0]
                )
                if most_frequent_row["label"] != "Missing":
                    missing_bucket = int(most_frequent_row["bucket_id"])
                else:
                    # missings are already the most common bucket, pick the next one
                    missing_bucket = int(
                        self.bucket_tables_[feature]
                        .sort_values("Count", ascending=False)
                        .reset_index(drop=True)["bucket_id"][1]
                    )
                # Repeat above procedure now we know the bucket distribution
                features_bucket_mapping_[feature] = BucketMapping(
                    feature_name=feature,
                    type="numerical",
                    map=splits,
                    right=False,  # note this is not default!
                    specials=special,
                    missing_bucket=missing_bucket,
                )

                # Recalculate the bucket table with the new bucket for missings
                self.bucket_tables_[feature] = build_bucket_table(
                    X,
                    y,
                    column=feature,
                    bucket_mapping=features_bucket_mapping_.get(feature),
                )

        self.features_bucket_mapping_ = FeaturesBucketMapping(features_bucket_mapping_)

        self._generate_summary(X, y)

        return self


class OrdinalCategoricalBucketer(BaseBucketer):
    """
    The `OrdinalCategoricalBucketer` replaces categories by ordinal numbers.

    Support ![badge](https://img.shields.io/badge/numerical-false-red) ![badge](https://img.shields.io/badge/categorical-true-green) ![badge](https://img.shields.io/badge/supervised-true-green)

    When `sort_by_target` is `false` the buckets are assigned in order of frequency.
    When `sort_by_target` is `true` the buckets are ordered based on the mean of the target per category.

    For example, if for a variable `colour` the means of the target
    for `blue`, `red` and `grey` is `0.5`, `0.8` and `0.1` respectively,
    `grey` will be the first bucket (`0`), blue the second (`1`) and
    `red` the third (`3`). If new data contains unknown labels (f.e. yellow),
    they will be replaced by the 'Other' bucket (`-2`),
    and if new data contains missing values, they will be replaced by the 'Missing' bucket (`-1`).

    Example:

    ```python
    from skorecard import datasets
    from skorecard.bucketers import OrdinalCategoricalBucketer

    X, y = datasets.load_uci_credit_card(return_X_y=True)
    bucketer = OrdinalCategoricalBucketer(variables=['EDUCATION'])
    bucketer.fit_transform(X, y)
    bucketer = OrdinalCategoricalBucketer(max_n_categories=2, variables=['EDUCATION'])
    bucketer.fit_transform(X, y)
    ```

    Credits: Code & ideas adapted from:

    - feature_engine.categorical_encoders.OrdinalCategoricalEncoder
    - feature_engine.categorical_encoders.RareLabelCategoricalEncoder

    """  # noqa

    def __init__(
        self,
        tol=0.05,
        max_n_categories=None,
        variables=[],
        specials={},
        encoding_method="frequency",
        missing_treatment="separate",
        remainder="passthrough",
    ):
        """
        Init the class.

        Args:
            tol (float): the minimum frequency a label should have to be considered frequent.
                Categories with frequencies lower than tol will be grouped together (in the highest ).
            max_n_categories (int): the maximum number of categories that should be considered frequent.
                If None, all categories with frequency above the tolerance (tol) will be
                considered.
            variables (list): The features to bucket. Uses all features if not defined.
            specials (dict): (nested) dictionary of special values that require their own binning.
                The dictionary has the following format:
                 {"<column name>" : {"name of special bucket" : <list with 1 or more values>}}
                For every feature that needs a special value, a dictionary must be passed as value.
                This dictionary contains a name of a bucket (key) and an array of unique values that should be put
                in that bucket.
                When special values are defined, they are not considered in the fitting procedure.
            encoding_method (string): encoding method.
                - "frequency" (default): orders the buckets based on the frequency of observations in the bucket.
                    The lower the number of the bucket the most frequent are the observations in that bucket.
                - "ordered": orders the buckets based on the average class 1 rate in the bucket.
                    The lower the number of the bucket the lower the fraction of class 1 in that bucket.
            missing_treatment (str or dict): Defines how we treat the missing values present in the data.
                If a string, it must be in ['separate', 'most_risky', 'most_frequent']
                    separate: Missing values get put in a separate 'Other' bucket: `-1`
                    most_risky: not yet implemented
                    most_frequent: not yet implemented
                If a dict, it must be of the following format:
                    {"<column name>": <bucket_number>}
                    This bucket number is where we will put the missing values.
            remainder (str): How we want the non-specified columns to be transformed. It must be in ["passthrough", "drop"].
                passthrough (Default): all columns that were not specified in "variables" will be passed through.
                drop: all remaining columns that were not specified in "variables" will be dropped.
        """  # noqa
        assert isinstance(variables, list)
        assert encoding_method in ["frequency", "ordered"]
        assert remainder in ["passthrough", "drop"]
        self._is_allowed_missing_treatment(missing_treatment)

        if tol < 0 or tol > 1:
            raise ValueError("tol takes values between 0 and 1")

        if max_n_categories is not None:
            if max_n_categories < 0 or not isinstance(max_n_categories, int):
                raise ValueError("max_n_categories takes only positive integer numbers")

        self.tol = tol
        self.max_n_categories = max_n_categories
        self.variables = variables
        self.specials = specials
        self.encoding_method = encoding_method
        self.missing_treatment = missing_treatment
        self.remainder = remainder

    def fit(self, X, y):
        """Init the class."""
        assert y is not None, "This bucketer is supervised. Provide y."
        X = self._is_dataframe(X)
        self.variables = self._check_variables(X, self.variables)
        self._verify_specials_variables(self.specials, X.columns)

        features_bucket_mapping_ = {}
        self.bucket_tables_ = {}

        for var in self.variables:

            normalized_counts = None
            # Determine the order of unique values

            if var in self.specials.keys():
                special = self.specials[var]
                X_flt, y_flt = self._filter_specials_for_fit(
                    X=X[var], y=y, specials=special
                )
            else:
                X_flt, y_flt = X[var], y
                special = {}
            if not (isinstance(y_flt, pd.Series) or isinstance(y_flt, pd.DataFrame)):
                y_flt = pd.Series(y_flt)

            X_flt, y_flt = self._filter_na_for_fit(X=X_flt, y=y_flt)

            X_y = pd.concat([X_flt, y_flt], axis=1)
            X_y.columns = [var, "target"]

            if self.encoding_method == "ordered":
                if y is None:
                    raise ValueError(
                        "To use encoding_method=='ordered', y cannot be None."
                    )
                # X_flt["target"] = y_flt
                normalized_counts = X_y[var].value_counts(normalize=True)
                cats = (
                    X_y.groupby([var])["target"]
                    .mean()
                    .sort_values(ascending=True)
                    .index
                )
                normalized_counts = normalized_counts[cats]

            if self.encoding_method == "frequency":
                normalized_counts = X_y[var].value_counts(normalize=True)

            # Limit number of categories if set.
            normalized_counts = normalized_counts[: self.max_n_categories]

            # Remove less frequent categories
            normalized_counts = normalized_counts[normalized_counts >= self.tol]

            # Determine Ordinal Encoder based on ordered labels
            # Note we start at 1, to be able to encode missings as 0.
            mapping = dict(
                zip(normalized_counts.index, range(0, len(normalized_counts)))
            )

            # Deal with missing values
            if self.missing_treatment in ["separate", "most_frequent"]:
                missing_bucket = None
            if isinstance(self.missing_treatment, dict):
                missing_bucket = self.missing_treatment.get(var)

            features_bucket_mapping_[var] = BucketMapping(
                feature_name=var,
                type="categorical",
                map=mapping,
                specials=special,
                missing_bucket=missing_bucket,
            )

            # Calculate the bucket table
            self.bucket_tables_[var] = build_bucket_table(
                X, y, column=var, bucket_mapping=features_bucket_mapping_.get(var)
            )

            if self.missing_treatment == "most_frequent":
                most_frequent_row = (
                    self.bucket_tables_[var]
                    .sort_values("Count", ascending=False)
                    .reset_index(drop=True)
                    .iloc[0]
                )
                if most_frequent_row["label"] != "Missing":
                    missing_bucket = int(most_frequent_row["bucket_id"])
                else:
                    # missings are already the most common bucket, pick the next one
                    missing_bucket = int(
                        self.bucket_tables_[var]
                        .sort_values("Count", ascending=False)
                        .reset_index(drop=True)["bucket_id"][1]
                    )
                # Repeat above procedure now we know the bucket distribution
                features_bucket_mapping_[var] = BucketMapping(
                    feature_name=var,
                    type="categorical",
                    map=mapping,
                    specials=special,
                    missing_bucket=missing_bucket,
                )

                # Recalculate the bucket table with the new bucket for missings
                self.bucket_tables_[var] = build_bucket_table(
                    X, y, column=var, bucket_mapping=features_bucket_mapping_.get(var)
                )

        self.features_bucket_mapping_ = FeaturesBucketMapping(features_bucket_mapping_)

        self._generate_summary(X, y)

        return self


class AsIsCategoricalBucketer(BaseBucketer):
    """
    The `AsIsCategoricalBucketer` treats unique values as categories.

    Support: ![badge](https://img.shields.io/badge/numerical-false-red) ![badge](https://img.shields.io/badge/categorical-true-green) ![badge](https://img.shields.io/badge/supervised-false-blue)

    It will assign each a bucket number in the order of appearance.
    If new data contains new, unknown labels they will be replaced by 'Other'.

    This is bucketer is useful when you have data that is already sufficiented bucketed,
    but you would like to be able to bucket new data in the same way.

    Example:

    ```python
    from skorecard import datasets
    from skorecard.bucketers import AsIsCategoricalBucketer

    X, y = datasets.load_uci_credit_card(return_X_y=True)
    bucketer = AsIsCategoricalBucketer(variables=['EDUCATION'])
    bucketer.fit_transform(X)
    ```
    """  # noqa

    def __init__(
        self,
        variables=[],
        specials={},
        missing_treatment="separate",
        remainder="passthrough",
    ):
        """Init the class.

        Args:
            variables (list): The features to bucket. Uses all features if not defined.
            specials: (nested) dictionary of special values that require their own binning.
                The dictionary has the following format:
                 {"<column name>" : {"name of special bucket" : <list with 1 or more values>}}
                For every feature that needs a special value, a dictionary must be passed as value.
                This dictionary contains a name of a bucket (key) and an array of unique values that should be put
                in that bucket.
                When special values are defined, they are not considered in the fitting procedure.
            missing_treatment: Defines how we treat the missing values present in the data.
                If a string, it must be in ['separate', 'most_risky', 'most_frequent']
                    separate: Missing values get put in a separate 'Other' bucket: `-1`
                    most_risky: not yet implemented
                    most_frequent: not yet implemented
                If a dict, it must be of the following format:
                    {"<column name>": <bucket_number>}
                    This bucket number is where we will put the missing values.
            remainder: How we want the non-specified columns to be transformed. It must be in ["passthrough", "drop"].
                passthrough (Default): all columns that were not specified in "variables" will be passed through.
                drop: all remaining columns that were not specified in "variables" will be dropped.
        """
        assert isinstance(variables, list)
        assert remainder in ["passthrough", "drop"]
        self._is_allowed_missing_treatment(missing_treatment)

        self.variables = variables
        self.specials = specials
        self.missing_treatment = missing_treatment
        self.remainder = remainder

    def fit(self, X, y=None):
        """Init the class."""
        X = self._is_dataframe(X)
        self.variables = self._check_variables(X, self.variables)
        self._verify_specials_variables(self.specials, X.columns)

        features_bucket_mapping_ = {}
        self.bucket_tables_ = {}

        for var in self.variables:

            if var in self.specials.keys():
                special = self.specials[var]
                X_flt, y_flt = self._filter_specials_for_fit(
                    X=X[var], y=y, specials=special
                )
            else:
                X_flt, y_flt = X[var], y
                special = {}
            if not (isinstance(y_flt, pd.Series) or isinstance(y_flt, pd.DataFrame)):
                y_flt = pd.Series(y_flt)

            X_flt, y_flt = self._filter_na_for_fit(X=X_flt, y=y_flt)

            unq = X_flt.unique().tolist()
            mapping = dict(zip(unq, range(0, len(unq))))

            # Deal with missing values
            if self.missing_treatment in ["separate", "most_frequent"]:
                missing_bucket = None
            if isinstance(self.missing_treatment, dict):
                missing_bucket = self.missing_treatment.get(var)

            features_bucket_mapping_[var] = BucketMapping(
                feature_name=var,
                type="categorical",
                map=mapping,
                specials=special,
                missing_bucket=missing_bucket,
            )

            # Calculate the bucket table
            self.bucket_tables_[var] = build_bucket_table(
                X, y, column=var, bucket_mapping=features_bucket_mapping_.get(var)
            )

            if self.missing_treatment == "most_frequent":
                most_frequent_row = (
                    self.bucket_tables_[var]
                    .sort_values("Count", ascending=False)
                    .reset_index(drop=True)
                    .iloc[0]
                )
                if most_frequent_row["label"] != "Missing":
                    missing_bucket = int(most_frequent_row["bucket_id"])
                else:
                    # missings are already the most common bucket, pick the next one
                    missing_bucket = int(
                        self.bucket_tables_[var]
                        .sort_values("Count", ascending=False)
                        .reset_index(drop=True)["bucket_id"][1]
                    )
                # Repeat above procedure now we know the bucket distribution
                features_bucket_mapping_[var] = BucketMapping(
                    feature_name=var,
                    type="categorical",
                    map=mapping,
                    specials=special,
                    missing_bucket=missing_bucket,
                )

                # Recalculate the bucket table with the new bucket for missings
                self.bucket_tables_[var] = build_bucket_table(
                    X, y, column=var, bucket_mapping=features_bucket_mapping_.get(var)
                )

        self.features_bucket_mapping_ = FeaturesBucketMapping(features_bucket_mapping_)

        self._generate_summary(X, y)

        return self


class AsIsNumericalBucketer(BaseBucketer):
    """
    The `AsIsNumericalBucketer` transformer creates buckets by treating the existing unique values as boundaries.

    Support: ![badge](https://img.shields.io/badge/numerical-true-green) ![badge](https://img.shields.io/badge/categorical-false-red) ![badge](https://img.shields.io/badge/supervised-false-blue)

    This is bucketer is useful when you have data that is already sufficiented bucketed,
    but you would like to be able to bucket new data in the same way.

    Example:

    ```python
    from skorecard import datasets
    from skorecard.bucketers import AsIsNumericalBucketer

    X, y = datasets.load_uci_credit_card(return_X_y=True)
    bucketer = AsIsNumericalBucketer(variables=['LIMIT_BAL'])
    bucketer.fit_transform(X)
    ```
    """  # noqa

    def __init__(
        self,
        right=True,
        variables=[],
        specials={},
        missing_treatment="separate",
        remainder="passthrough",
    ):
        """
        Init the class.

        Args:
            right (boolean): Is the right value included in a range (default) or is 'up to not but including'.
                For example, if you have [5, 10], the ranges for right=True would be (-Inf, 5], (5, 10], (10, Inf]
                or [-Inf, 5), [5, 10), [10, Inf) for right=False
            variables (list): The features to bucket. Uses all features if not defined.
            specials (dict): (nested) dictionary of special values that require their own binning.
                The dictionary has the following format:
                 {"<column name>" : {"name of special bucket" : <list with 1 or more values>}}
                For every feature that needs a special value, a dictionary must be passed as value.
                This dictionary contains a name of a bucket (key) and an array of unique values that should be put
                in that bucket.
                When special values are defined, they are not considered in the fitting procedure.
            missing_treatment (str or dict): Defines how we treat the missing values present in the data.
                If a string, it must be in ['separate', 'most_risky', 'most_frequent']
                    separate: Missing values get put in a separate 'Other' bucket: `-1`
                    most_risky: not yet implemented
                    most_frequent: not yet implemented
                If a dict, it must be of the following format:
                    {"<column name>": <bucket_number>}
                    This bucket number is where we will put the missing values..
            remainder (str): How we want the non-specified columns to be transformed. It must be in ["passthrough", "drop"].
                passthrough (Default): all columns that were not specified in "variables" will be passed through.
                drop: all remaining columns that were not specified in "variables" will be dropped.
        """  # noqa
        assert isinstance(variables, list)
        assert remainder in ["passthrough", "drop"]
        self._is_allowed_missing_treatment(missing_treatment)

        self.right = right
        self.variables = variables
        self.specials = specials
        self.missing_treatment = missing_treatment
        self.remainder = remainder

    def fit(self, X, y=None):
        """
        Fit X, y.
        """
        X = self._is_dataframe(X)
        self.variables = self._check_variables(X, self.variables)
        self._verify_specials_variables(self.specials, X.columns)

        features_bucket_mapping_ = {}
        self.bucket_tables_ = {}

        for feature in self.variables:

            if feature in self.specials.keys():
                special = self.specials[feature]
                X_flt, y_flt = self._filter_specials_for_fit(
                    X=X[feature], y=y, specials=special
                )
            else:
                X_flt = X[feature]
                special = {}

            boundaries = X_flt.unique().tolist()
            boundaries.sort()

            if len(boundaries) > 100:
                msg = f"The column '{feature}' has more than 100 unique values "
                msg += "and cannot be used with the AsIsBucketer."
                msg += "Apply a different bucketer first."
                raise NotPreBucketedError(msg)

            # Deal with missing values
            if self.missing_treatment in ["separate", "most_frequent"]:
                missing_bucket = None
            if isinstance(self.missing_treatment, dict):
                missing_bucket = self.missing_treatment.get(feature)

            features_bucket_mapping_[feature] = BucketMapping(
                feature_name=feature,
                type="numerical",
                map=boundaries,
                right=self.right,
                specials=special,
                missing_bucket=missing_bucket,
            )

            # Calculate the bucket table
            self.bucket_tables_[feature] = build_bucket_table(
                X,
                y,
                column=feature,
                bucket_mapping=features_bucket_mapping_.get(feature),
            )

            if self.missing_treatment == "most_frequent":
                most_frequent_row = (
                    self.bucket_tables_[feature]
                    .sort_values("Count", ascending=False)
                    .reset_index(drop=True)
                    .iloc[0]
                )
                if most_frequent_row["label"] != "Missing":
                    missing_bucket = int(most_frequent_row["bucket_id"])
                else:
                    # missings are already the most common bucket, pick the next one
                    missing_bucket = int(
                        self.bucket_tables_[feature]
                        .sort_values("Count", ascending=False)
                        .reset_index(drop=True)["bucket_id"][1]
                    )
                # Repeat above procedure now we know the bucket distribution
                features_bucket_mapping_[feature] = BucketMapping(
                    feature_name=feature,
                    type="numerical",
                    map=boundaries,
                    right=self.right,
                    specials=special,
                    missing_bucket=missing_bucket,
                )

                # Recalculate the bucket table with the new bucket for missings
                self.bucket_tables_[feature] = build_bucket_table(
                    X,
                    y,
                    column=feature,
                    bucket_mapping=features_bucket_mapping_.get(feature),
                )

        self.features_bucket_mapping_ = FeaturesBucketMapping(features_bucket_mapping_)

        self._generate_summary(X, y)

        return self


class UserInputBucketer(BaseBucketer):
    """
    The `UserInputBucketer` transformer creates buckets by implementing user-defined boundaries.

    Support: ![badge](https://img.shields.io/badge/numerical-true-green) ![badge](https://img.shields.io/badge/categorical-true-green) ![badge](https://img.shields.io/badge/supervised-false-blue)

    This is a special bucketer that is not fitted but rather relies
    on pre-defined user input. The most common use-case is loading
    bucket mapping information previously fitted by other bucketers.

    Example:

    ```python
    from skorecard import datasets
    from skorecard.bucketers import AgglomerativeClusteringBucketer, UserInputBucketer

    X, y = datasets.load_uci_credit_card(return_X_y=True)

    ac_bucketer = AgglomerativeClusteringBucketer(n_bins=3, variables=['LIMIT_BAL'])
    ac_bucketer.fit(X)
    mapping = ac_bucketer.features_bucket_mapping_

    ui_bucketer = UserInputBucketer(mapping)
    new_X = ui_bucketer.fit_transform(X)
    assert len(new_X['LIMIT_BAL'].unique()) == 3

    #Map some values to the special buckets
    specials = {
        "LIMIT_BAL":{
            "=50000":[50000],
            "in [20001,30000]":[20000,30000],
            }
    }

    ac_bucketer = AgglomerativeClusteringBucketer(n_bins=3, variables=['LIMIT_BAL'], specials = specials)
    ac_bucketer.fit(X)
    mapping = ac_bucketer.features_bucket_mapping_

    ui_bucketer = UserInputBucketer(mapping)
    new_X = ui_bucketer.fit_transform(X)
    assert len(new_X['LIMIT_BAL'].unique()) == 5
    ```

    """  # noqa

    def __init__(
        self,
        features_bucket_mapping: Union[Dict, FeaturesBucketMapping],
        variables: List = [],
    ) -> None:
        """Initialise the user-defined boundaries with a dictionary.

        Notes:
        - features_bucket_mapping is stored without the trailing underscore (_) because it is not fitted.

        Args:
            features_bucket_mapping (dict): Contains the feature name and boundaries defined for this feature.
                Either dict or FeaturesBucketMapping
            variables (list): The features to bucket. Uses all features in features_bucket_mapping if not defined.
        """
        # Assigning the variable in the init to the attribute with the same name is a requirement of
        # sklearn.base.BaseEstimator. See the notes in
        # https://scikit-learn.org/stable/modules/generated/sklearn.base.BaseEstimator.html#sklearn.base.BaseEstimator
        self.features_bucket_mapping = features_bucket_mapping

        # Check user defined input for bucketing. If a dict is specified, will auto convert
        if not isinstance(self.features_bucket_mapping, FeaturesBucketMapping):
            if not isinstance(self.features_bucket_mapping, dict):
                raise TypeError(
                    "'features_bucket_mapping' must be a dict or FeaturesBucketMapping instance"
                )
            self.features_bucket_mapping_ = FeaturesBucketMapping(
                self.features_bucket_mapping
            )
        else:
            self.features_bucket_mapping_ = self.features_bucket_mapping

        # If user did not specify any variables,
        # use all the variables defined in the features_bucket_mapping
        self.variables = variables
        if variables == []:
            self.variables = list(self.features_bucket_mapping_.maps.keys())

        # Set passthrough remainder for transform()
        self.remainder = "passthrough"

    def fit(self, X, y=None):
        """Init the class."""
        # bucket tables can only be computed on fit().
        # so a user will have to .fit() if she/he wants .plot_buckets() and .bucket_table()
        self.bucket_tables_ = {}
        for feature in self.variables:
            # Calculate the bucket table
            self.bucket_tables_[feature] = build_bucket_table(
                X,
                y,
                column=feature,
                bucket_mapping=self.features_bucket_mapping_.get(feature),
            )

        self._generate_summary(X, y)

        return self