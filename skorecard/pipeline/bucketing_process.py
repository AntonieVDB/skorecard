import pathlib
import pandas as pd

from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.utils.validation import check_is_fitted
from sklearn.pipeline import make_pipeline

from skorecard.utils import NotPreBucketedError, NotBucketedError
from skorecard.pipeline import to_skorecard_pipeline
from skorecard.pipeline.pipeline import _get_all_steps
from skorecard.bucketers import UserInputBucketer, DecisionTreeBucketer, OptimalBucketer
from skorecard.reporting import build_bucket_table
from skorecard.reporting.report import BucketTableMethod, SummaryMethod
from skorecard.reporting.plotting import PlotBucketMethod, PlotPreBucketMethod
from skorecard.features_bucket_mapping import FeaturesBucketMapping, merge_features_bucket_mapping

from typing import Dict, TypeVar

PathLike = TypeVar("PathLike", str, pathlib.Path)


class BucketingProcess(
    BaseEstimator,
    TransformerMixin,
    BucketTableMethod,
    PlotBucketMethod,
    PlotPreBucketMethod,
    SummaryMethod,
):
    """
    A two-step bucketing pipeline allowing for pre-bucketing before bucketing.

    Often you want to pre-bucket features (f.e. to 100 buckets) before bucketing to a smaller set.
    This brings some additional challenges around propagating specials and defining a bucketer that is able to go from raw data to final bucket.
    This class facilicates the process and also provides all regular methods and attributes:

    - `.summary()`: See which columns are bucketed
    - `.plot_bucket()`: Plot buckets of a column
    - `.bucket_table()`: Table with buckets of a column
    - `.save_to_yaml()`: Save information necessary for bucketing to a YAML file
    - `.features_bucket_mapping_`: Access bucketing information

    Example:

    ```python
    from skorecard import datasets
    from skorecard.bucketers import DecisionTreeBucketer, OptimalBucketer, AsIsCategoricalBucketer
    from skorecard.pipeline import BucketingProcess
    from sklearn.pipeline import make_pipeline

    df = datasets.load_uci_credit_card(as_frame=True)
    y = df["default"]
    X = df.drop(columns=["default"])

    num_cols = ["LIMIT_BAL", "BILL_AMT1"]
    cat_cols = ["EDUCATION", "MARRIAGE"]

    bucketing_process = BucketingProcess(
        specials={'LIMIT_BAL': {'=400000.0' : [400000.0]}},
        prebucketing_pipeline=make_pipeline(
            DecisionTreeBucketer(variables=num_cols, max_n_bins=100, min_bin_size=0.05),
            AsIsCategoricalBucketer(variables=cat_cols),
        ),
        bucketing_pipeline=make_pipeline(
            OptimalBucketer(variables=num_cols, max_n_bins=10, min_bin_size=0.05),
            OptimalBucketer(variables=cat_cols, variables_type='categorical', max_n_bins=10, min_bin_size=0.05),
        )
    )

    bucketing_process.fit(X, y)

    # Details
    bucketing_process.summary() # all vars, and # buckets
    bucketing_process.bucket_table("LIMIT_BAL")
    bucketing_process.plot_bucket("LIMIT_BAL")
    bucketing_process.prebucket_table("LIMIT_BAL")
    bucketing_process.plot_prebucket("LIMIT_BAL")
    ```
    """  # noqa

    def __init__(
        self,
        prebucketing_pipeline=make_pipeline(DecisionTreeBucketer(max_n_bins=50, min_bin_size=0.02)),
        bucketing_pipeline=make_pipeline(OptimalBucketer(max_n_bins=6, min_bin_size=0.05)),
        specials={},
    ):
        """
        Init the class.

        Args:
            specials: (nested) dictionary of special values that require their own binning.
                The dictionary has the following format:
                 {"<column name>" : {"name of special bucket" : <list with 1 or more values>}}
                For every feature that needs a special value, a dictionary must be passed as value.
                This dictionary contains a name of a bucket (key) and an array of unique values that should be put
                in that bucket.
                When special values are defined, they are not considered in the fitting procedure.
            prebucketing_pipeline (Pipeline): The scikit-learn pipeline that does pre-bucketing.
                Defaults to an all-numeric DecisionTreeBucketer pipeline.
            bucketing_pipeline (Pipeline): The scikit-learn pipeline that does bucketing.
                Defaults to an all-numeric OptimalBucketer pipeline.
                Must transform same features as the prebucketing pipeline.
        """
        # Convert to skorecard pipelines
        # This does some checks on the pipelines
        # and adds some convenience methods to the pipeline.
        self.prebucketing_pipeline = to_skorecard_pipeline(prebucketing_pipeline)
        self.bucketing_pipeline = to_skorecard_pipeline(bucketing_pipeline)

        # Add/Overwrite specials to all pre-bucketers
        for step in _get_all_steps(self.prebucketing_pipeline):
            if len(step.specials) != 0:
                raise ValueError(
                    f"Specials should be defined on the BucketingProcess level, remove the specials from {step}"
                )
            step.specials = specials

        # Assigning the variable in the init to the attribute with the same name is a requirement of
        # sklearn.base.BaseEstimator. See the notes in
        # https://scikit-learn.org/stable/modules/generated/sklearn.base.BaseEstimator.html#sklearn.base.BaseEstimator
        self.specials = specials
        self._prebucketing_specials = self.specials
        self._bucketing_specials = dict()  # will be determined later.
        self.name = "bucketingprocess"  # to be able to identity the bucketingprocess in a pipeline

    def fit(self, X, y=None):
        """
        Fit the prebucketing and bucketing pipeline with `X`, `y`.

        Args:
            X (pd.DataFrame): Data to fit on.
            y (np.array, optional): target. Defaults to None.
        """
        # Fit the prebucketing pipeline
        X_prebucketed_ = self.prebucketing_pipeline.fit_transform(X, y)
        assert isinstance(X_prebucketed_, pd.DataFrame)

        # Calculate the prebucket tables.
        self.prebucket_tables_ = dict()
        for column in X.columns:
            if column in self.prebucketing_pipeline.features_bucket_mapping_.maps.keys():
                self.prebucket_tables_[column] = build_bucket_table(
                    X, y, column=column, bucket_mapping=self.prebucketing_pipeline.features_bucket_mapping_.get(column)
                )

        # Find the new bucket numbers of the specials after prebucketing,
        for var, var_specials in self._prebucketing_specials.items():
            bucket_labels = self.prebucketing_pipeline.features_bucket_mapping_.get(var).labels
            new_specials = _find_remapped_specials(bucket_labels, var_specials)
            if len(new_specials):
                self._bucketing_specials[var] = new_specials

        # Then assign the new specials to all bucketers in the bucketing pipeline
        for step in self.bucketing_pipeline.steps:
            if type(step) != tuple:
                step.specials = self._bucketing_specials
            else:
                step[1].specials = self._bucketing_specials

        # Fit the bucketing pipeline
        # And save the bucket mapping
        self.bucketing_pipeline.fit(X_prebucketed_, y)

        # Make sure all columns that are bucketed have also been pre-bucketed.
        not_prebucketed = []
        for col in self.bucketing_pipeline.features_bucket_mapping_.columns:
            if col not in self.prebucketing_pipeline.features_bucket_mapping_.columns:
                not_prebucketed.append(col)
        if len(not_prebucketed):
            msg = f"The following columns are bucketed but have not been pre-bucketed: {', '.join(not_prebucketed)}.\n"
            msg += "Consider adding an AsIsNumericalBucketer or AsIsCategoricalBucketer to the prebucketing pipeline.\n"
            msg += "Or add an additional bucketing step after the BucketingProcess:\n"
            msg += "make_pipeline(BucketingProcess(..), Bucketer())"
            raise NotPreBucketedError(msg)

        # Make sure all columns that have been pre-bucketed also have been bucketed
        not_bucketed = []
        for col in self.prebucketing_pipeline.features_bucket_mapping_.columns:
            if col not in self.bucketing_pipeline.features_bucket_mapping_.columns:
                not_bucketed.append(col)
        if len(not_bucketed):
            msg = f"The following columns are prebucketed but have not been bucketed: {', '.join(not_bucketed)}.\n"
            msg += "Consider updating the bucketing pipeline,\n"
            raise NotBucketedError(msg)

        # calculate the bucket tables.
        self.bucket_tables_ = dict()
        for column in X.columns:
            if column in self.bucketing_pipeline.features_bucket_mapping_.maps.keys():
                self.bucket_tables_[column] = build_bucket_table(
                    X_prebucketed_,
                    y,
                    column=column,
                    bucket_mapping=self.bucketing_pipeline.features_bucket_mapping_.get(column),
                )

        # Calculate the summary
        self._generate_summary(X, y)

        return self

    def _set_bucket_mapping(self, features_bucket_mapping, X_prebucketed, y):
        """
        Replace the bucket mapping in the bucketing_pipeline.

        This is meant for use internally in the dash app, where we manually edit
        `features_bucket_mapping_`.

        To be able to update the bucketingprocess, use something like:

        >>> X_prebucketed = bucketingprocess.prebucket_pipeline.transform(X)
        >>> feature_bucket_mapping # your edited bucketingprocess.features_bucket_mapping_
        >>> bucketingprocess._set_bucket_mapping(feature_bucket_mapping, X_prebucketed, y)
        """
        # Step 1: replace the bucketing pipeline with a UI bucketer that uses the new mapping
        self.bucketing_pipeline = UserInputBucketer(features_bucket_mapping)

        # Step 2: Recalculate the bucket tables
        # Step 3: Update summary table
        self.bucket_tables_ = dict()
        for column in X_prebucketed.columns:
            if column in self.bucketing_pipeline.features_bucket_mapping_.maps.keys():
                self.bucket_tables_[column] = build_bucket_table(
                    X_prebucketed,
                    y,
                    column=column,
                    bucket_mapping=self.bucketing_pipeline.features_bucket_mapping_.get(column),
                )
                assert column in self.summary_dict.keys()
                # Update bucket_number in summary table
                # See _generate_summary
                self.summary_dict[column][1] = len(self.bucket_tables_[column]["bucket_id"].unique())

    def transform(self, X):
        """
        Transform `X` through the prebucketing and bucketing pipelines.
        """
        check_is_fitted(self)
        X_prebucketed = self.prebucketing_pipeline.transform(X)
        return self.bucketing_pipeline.transform(X_prebucketed)

    def save_yml(self, fout: PathLike) -> None:
        """
        Save the features bucket to a yaml file.

        Args:
            fout: path for output file
        """
        check_is_fitted(self)
        fbm = self.features_bucket_mapping_
        if isinstance(fbm, dict):
            FeaturesBucketMapping(fbm).save_yml(fout)
        else:
            fbm.save_yml(fout)

    @property
    def features_bucket_mapping_(self):
        """
        Returns a `FeaturesBucketMapping` instance.

        In normal bucketers, you can access `.features_bucket_mapping_`
        to retrieve a `FeaturesBucketMapping` instance. This contains
        all the info you need to transform values into their buckets.

        In this class, we basically have a two step bucketing process:
        first prebucketing, and then we bucket the prebuckets.

        In order to still be able to use BucketingProcess as if it were a normal bucketer,
        we'll need to merge both into one.
        """
        check_is_fitted(self)
        # in .fit() we already make sure all columns that are prebucketed are bucketed, and vice versa
        # this assert is just to be very sure.
        assert len(self.prebucketing_pipeline.features_bucket_mapping_) == len(
            self.bucketing_pipeline.features_bucket_mapping_
        )

        return merge_features_bucket_mapping(
            self.prebucketing_pipeline.features_bucket_mapping_, self.bucketing_pipeline.features_bucket_mapping_
        )

    def prebucket_table(self, column: str) -> pd.DataFrame:
        """
        Generates the statistics for the buckets of a particular column.

        An example is seen below:

        pre-bucket | label      | Count | Count (%) | Non-event | Event | Event Rate | WoE   | IV   | bucket
        -----------|------------|-------|-----------|-----------|-------|------------|-------|------|------
        0          | (-inf, 1.0)| 479   | 7.98      | 300       | 179   |  37.37     |  0.73 | 0.05 | 0
        1          | [1.0, 2.0) | 370   | 6.17      | 233       | 137   |  37.03     |  0.71 | 0.04 | 0

        Args:
            column (str): The column we wish to analyse

        Returns:
            df (pd.DataFrame): A pandas dataframe of the format above
        """  # noqa
        check_is_fitted(self)
        if column not in self.prebucket_tables_.keys():
            raise ValueError(f"column '{column}' was not part of the pre-bucketing process")

        table = self.prebucket_tables_.get(column)
        table = table.rename(columns={"bucket_id": "pre-bucket"})

        # Apply bucket mapping
        bucket_mapping = self.bucketing_pipeline.features_bucket_mapping_.get(column)
        table["bucket"] = bucket_mapping.transform(table["pre-bucket"])
        return table


def _find_remapped_specials(bucket_labels: Dict, var_specials: Dict) -> Dict:
    """
    Remaps the specials after the prebucketing process.

    Basically, every bucketer in the bucketing pipeline will now need to
    use the prebucketing bucket as a different special value,
    because prebucketing put the specials into a bucket.

    Args:
        bucket_labels (dict): The label for each unique bucket of a variable
        var_specials (dict): The specials for a variable, if any.
    """
    if bucket_labels is None or var_specials is None:
        return {}

    new_specials = {}
    for label in var_specials.keys():
        for bucket, bucket_label in bucket_labels.items():
            if bucket_label == f"Special: {label}":
                new_specials[label] = [bucket]

    return new_specials
