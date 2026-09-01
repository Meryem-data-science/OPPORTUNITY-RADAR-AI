from dataclasses import replace

import pytest

from services.collector.matching import (
    MatchingBatchResult,
    MatchingPersistenceError,
    matching_batch_fingerprint,
    store_matching_batch,
)
from tests.unit.test_matching_engine import assessment


def batch(*items):
    result = MatchingBatchResult(tuple(items), "a" * 64, "b" * 64, len(items))
    return replace(result, batch_fingerprint=matching_batch_fingerprint(result))


def test_rejects_duplicate_ids_and_invalid_selection_before_database_use():
    item = assessment()
    with pytest.raises(MatchingPersistenceError, match="duplicate"):
        store_matching_batch(
            None, item.profile_id, batch(item, item), selection_version="test-v1"
        )
    with pytest.raises(MatchingPersistenceError, match="selection_version"):
        store_matching_batch(
            None, item.profile_id, batch(item), selection_version=" test "
        )


def test_rejects_semantic_fingerprint_mismatches_before_database_use():
    item = assessment()
    with pytest.raises(MatchingPersistenceError, match="assessment fingerprint"):
        store_matching_batch(
            None,
            item.profile_id,
            batch(replace(item, lane=item.lane.UNCERTAIN)),
            selection_version="test-v1",
        )
    valid = batch(item)
    with pytest.raises(MatchingPersistenceError, match="batch fingerprint"):
        store_matching_batch(
            None,
            item.profile_id,
            replace(valid, batch_fingerprint="f" * 64),
            selection_version="test-v1",
        )
