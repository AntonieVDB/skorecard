import pytest
import numpy as np

from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline
from sklearn.utils.validation import check_is_fitted
from sklearn.exceptions import NotFittedError

from skorecard.bucketers.bucketers import UserInputBucketer
from skorecard.bucketers import (
    EqualWidthBucketer,
    AgglomerativeClusteringBucketer,
    EqualFrequencyBucketer,
    OptimalBucketer,
    OrdinalCategoricalBucketer,
    AsIsCategoricalBucketer,
    AsIsNumericalBucketer,
    DecisionTreeBucketer,
)
from skorecard.pipeline import BucketingProcess

BUCKETERS_WITH_SET_BINS = [
    EqualWidthBucketer,
    AgglomerativeClusteringBucketer,
    EqualFrequencyBucketer,
]

BUCKETERS_WITHOUT_SET_BINS = [
    OptimalBucketer,
    OrdinalCategoricalBucketer,
    AsIsNumericalBucketer,
    AsIsCategoricalBucketer,
    DecisionTreeBucketer,
]

# Except the very special UserInputBucketer of course :)
ALL_BUCKETERS = BUCKETERS_WITH_SET_BINS + BUCKETERS_WITHOUT_SET_BINS
ALL_BUCKETERS_WITH_BUCKETPROCESS = ALL_BUCKETERS + [BucketingProcess]


@pytest.mark.parametrize("bucketer", BUCKETERS_WITH_SET_BINS)
def test_single_bucket(bucketer, df) -> None:
    """Test that using n_bins=1 puts everything into 1 bucket."""
    BUCK = bucketer(n_bins=1, variables=["MARRIAGE"])
    x_t = BUCK.fit_transform(df)
    assert len(x_t["MARRIAGE"].unique()) == 1


@pytest.mark.parametrize("bucketer", BUCKETERS_WITH_SET_BINS)
def test_two_buckets(bucketer, df) -> None:
    """Test that using n_bins=1 puts everything into 2 buckets."""
    X = df
    y = df["default"].values

    BUCK = bucketer(n_bins=2, variables=["MARRIAGE"])
    BUCK.fit(X, y)
    x_t = BUCK.transform(X)
    assert len(x_t["MARRIAGE"].unique()) == 2


@pytest.mark.parametrize("bucketer", BUCKETERS_WITH_SET_BINS)
def test_three_bins(bucketer, df) -> None:
    """Test that we get the number of bins we request."""
    # Test single bin counts
    BUCK = bucketer(n_bins=3, variables=["MARRIAGE"])
    x_t = BUCK.fit_transform(df)
    assert len(x_t["MARRIAGE"].unique()) == 3


@pytest.mark.parametrize("bucketer", BUCKETERS_WITH_SET_BINS)
def test_error_input(bucketer):
    """Test that a non-int leads to problems in bins."""
    with pytest.raises(AssertionError):
        bucketer(n_bins=[2])

    with pytest.raises(AssertionError):
        bucketer(n_bins=4.2, variables=["MARRIAGE"])


@pytest.mark.parametrize("bucketer", BUCKETERS_WITH_SET_BINS)
def test_missing_default(bucketer, df_with_missings) -> None:
    """Test that missing values are assigned to the right bucket."""
    X = df_with_missings
    y = df_with_missings["default"].values

    BUCK = bucketer(n_bins=2, variables=["MARRIAGE"])
    BUCK.fit(X, y)
    X["MARRIAGE_trans"] = BUCK.transform(X[["MARRIAGE"]])
    assert len(X["MARRIAGE_trans"].unique()) == 3
    assert X[np.isnan(X["MARRIAGE"])].shape[0] == X[X["MARRIAGE_trans"] == -1].shape[0]


@pytest.mark.parametrize("bucketer", BUCKETERS_WITH_SET_BINS)
def test_missing_manual(bucketer, df_with_missings) -> None:
    """Test that missing values are assigned to the right bucket when manually given."""
    X = df_with_missings
    y = df_with_missings["default"].values

    BUCK = bucketer(n_bins=3, variables=["MARRIAGE", "LIMIT_BAL"], missing_treatment={"LIMIT_BAL": 1, "MARRIAGE": 0})
    BUCK.fit(X, y)
    X_trans = BUCK.transform(X[["MARRIAGE", "LIMIT_BAL"]])
    assert len(X_trans["MARRIAGE"].unique()) == 3
    assert len(X_trans["LIMIT_BAL"].unique()) == 3

    X["MARRIAGE_TRANS"] = X_trans["MARRIAGE"]
    assert X[np.isnan(X["MARRIAGE"])]["MARRIAGE_TRANS"].sum() == 0  # Sums to 0 as they are all in bucket 0

    assert "| Missing" in [f for f in BUCK.features_bucket_mapping_.get("MARRIAGE").labels.values()][0]
    assert "| Missing" in [f for f in BUCK.features_bucket_mapping_.get("LIMIT_BAL").labels.values()][1]


@pytest.mark.parametrize("bucketer", BUCKETERS_WITH_SET_BINS)
def test_missing_most_frequent_set(bucketer, df_with_missings) -> None:
    """Test that missing values are assigned to the right bucket when manually given."""
    X = df_with_missings
    y = df_with_missings["default"].values

    BUCK = bucketer(n_bins=3, variables=["MARRIAGE", "LIMIT_BAL"], missing_treatment="most_frequent")
    BUCK.fit(X, y)

    for feature in ["MARRIAGE", "LIMIT_BAL"]:
        assert (
            "Missing"
            in BUCK.bucket_table(feature).sort_values("Count", ascending=False).reset_index(drop=True)["label"][0]
        )


@pytest.mark.parametrize("bucketer", BUCKETERS_WITHOUT_SET_BINS)
def test_missing_most_frequent_withoutset(bucketer, df_with_missings) -> None:
    """Test that missing values are assigned to the right bucket when manually given."""
    X = df_with_missings
    y = df_with_missings["default"].values

    BUCK = bucketer(variables=["MARRIAGE", "EDUCATION"], missing_treatment="most_frequent")
    BUCK.fit(X, y)

    for feature in ["MARRIAGE", "EDUCATION"]:
        assert (
            "Missing"
            in BUCK.bucket_table(feature).sort_values("Count", ascending=False).reset_index(drop=True)["label"][0]
        )


@pytest.mark.parametrize("bucketer", ALL_BUCKETERS)
def test_type_error_input(bucketer, df):
    """Test that input is always a dataFrame."""
    df = df.drop(columns=["pet_ownership"])
    pipe = make_pipeline(
        StandardScaler(),
        bucketer(variables=["BILL_AMT1"]),
    )
    with pytest.raises(TypeError):
        pipe.fit_transform(df)


@pytest.mark.parametrize("bucketer", ALL_BUCKETERS)
def test_is_not_fitted(bucketer):
    """
    Make sure we didn't make any mistakes when building a bucketer.
    """
    BUCK = bucketer()
    with pytest.raises(NotFittedError):
        check_is_fitted(BUCK)


@pytest.mark.parametrize("bucketer", ALL_BUCKETERS)
def test_ui_bucketer(bucketer, df):
    """
    Make sure we didn't make any mistakes when building a bucketer.
    """
    BUCK = bucketer()
    # we drop BILL_AMT1 because that one needs prebucketing for some bucketers.
    df = df.drop(columns=["pet_ownership", "BILL_AMT1"])
    X = df
    y = df["default"].values
    X_trans = BUCK.fit_transform(X, y)

    uib = UserInputBucketer(BUCK.features_bucket_mapping_)
    assert X_trans.equals(uib.transform(X))


@pytest.mark.parametrize("bucketer", ALL_BUCKETERS)
def test_zero_indexed(bucketer, df):
    """Test that bins are zero-indexed.

    When no missing values are present, no specials defined,
    bucket transforms should be zero indexed.

    Note that -2 (for 'other') is also allowed,
    f.e. OrdinalCategoricalBucketer puts less frequents cats there.
    """
    BUCK = bucketer()

    y = df["default"].values
    # we drop BILL_AMT1 because that one needs prebucketing for some bucketers.
    x_t = BUCK.fit_transform(df.drop(columns=["pet_ownership", "BILL_AMT1"]), y)
    assert x_t["MARRIAGE"].min() in [0, -2]
    assert x_t["EDUCATION"].min() in [0, -2]
    assert x_t["LIMIT_BAL"].min() in [0, -2]


@pytest.mark.parametrize("bucketer", ALL_BUCKETERS)
def test_remainder_argument_no_bins(bucketer, df):
    """Test remainder argument works."""
    BUCK = bucketer(variables=["LIMIT_BAL"], remainder="drop")
    X = df
    y = df["default"].values

    BUCK.fit(X, y)
    X_trans = BUCK.transform(X)
    assert X_trans.columns == "LIMIT_BAL"

    BUCK = bucketer(variables=["LIMIT_BAL"], remainder="passthrough")
    BUCK.fit(X, y)
    X_trans = BUCK.transform(X)
    assert set(X_trans.columns) == set(X.columns)


@pytest.mark.parametrize("bucketer", ALL_BUCKETERS)
def test_summary_no_bins(bucketer, df):
    """Test summary works."""
    BUCK = bucketer(variables=["LIMIT_BAL"], remainder="passthrough")
    X = df
    y = df["default"].values
    BUCK.fit(X, y)
    summary_table = BUCK.summary()
    assert summary_table.shape[0] == 6
    assert set(summary_table.columns) == set(["column", "num_prebuckets", "num_buckets", "IV_score", "dtype"])
