import pytest
from jina.hub.rankers.MaxRanker import MaxRanker
from jina.hub.rankers.MinRanker import MinRanker

from jina.drivers.rank import Chunk2DocRankDriver
from jina.executors.rankers import Chunk2DocRanker
from jina.proto import jina_pb2, uid


class MockLengthRanker(Chunk2DocRanker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.required_keys = {'length'}

    def _get_score(self, match_idx, query_chunk_meta, match_chunk_meta, *args, **kwargs):
        return match_idx[0][self.col_doc_id], match_chunk_meta[match_idx[0][self.col_chunk_id]]['length']


class SimpleChunk2DocRankDriver(Chunk2DocRankDriver):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @property
    def exec_fn(self):
        return self._exec_fn


def create_document_to_score():
    # doc: 1
    # |- chunk: 2
    # |  |- matches: (id: 4, parent_id: 40, score.value: 4),
    # |  |- matches: (id: 5, parent_id: 50, score.value: 5),
    # |
    # |- chunk: 3
    #    |- matches: (id: 6, parent_id: 60, score.value: 6),
    #    |- matches: (id: 7, parent_id: 70, score.value: 7)
    doc = jina_pb2.Document()
    doc.tags['id'] = 1
    for c in range(2):
        chunk = doc.chunks.add()
        chunk.tags['id'] = doc.tags['id'] + c + 1
        chunk.id = uid.new_doc_id(chunk)
        for m in range(2):
            match = chunk.matches.add()
            match.tags['id'] = 2 * chunk.tags['id'] + m
            match.parent_id = uid.new_doc_id(match)
            match.length = m
            # to be used by MaxRanker and MinRanker
            match.score.ref_id = chunk.id
            match.score.value = match.tags['id']
    return doc


def create_chunk_matches_to_score():
    # doc: (id: 100, granularity=0)
    # |- chunks: (id: 10)
    # |  |- matches: (id: 11, parent_id: 1, score.value: 2),
    # |  |- matches: (id: 12, parent_id: 1, score.value: 3),
    # |- chunks: (id: 20)
    #    |- matches: (id: 21, parent_id: 2, score.value: 4),
    #    |- matches: (id: 22, parent_id: 2, score.value: 5)
    doc = jina_pb2.Document()
    doc.id = 100
    doc.granularity = 0
    num_matches = 2
    for parent_id in range(1, 3):
        chunk = doc.chunks.add()
        chunk.id = parent_id * 10
        chunk.granularity = doc.granularity + 1
        for score_value in range(parent_id * 2, parent_id * 2 + num_matches):
            match = chunk.matches.add()
            match.granularity = chunk.granularity
            match.parent_id = parent_id
            match.score.value = score_value
            match.score.ref_id = chunk.id
            match.id = 10 * parent_id + score_value
            match.length = 4
    return doc


def create_chunk_chunk_matches_to_score():
    # doc: (id: 100, granularity=0)
    # |- chunk: (id: 101, granularity=1)
    #       |- chunks: (id: 10)
    #       |   |- matches: (id: 11, parent_id: 1, score.value: 2),
    #       |   |- matches: (id: 12, parent_id: 1, score.value: 3),
    #       |- chunks: (id: 20)
    #           |- matches: (id: 21, parent_id: 2, score.value: 4),
    #           |- matches: (id: 22, parent_id: 2, score.value: 5)
    doc = jina_pb2.Document()
    doc.id = 100
    doc.granularity = 0
    chunk = doc.chunks.add()
    chunk.id = 101
    chunk.granularity = doc.granularity + 1
    num_matches = 2
    for parent_id in range(1, 3):
        chunk_chunk = chunk.chunks.add()
        chunk_chunk.id = parent_id * 10
        chunk_chunk.granularity = chunk.granularity + 1
        for score_value in range(parent_id * 2, parent_id * 2 + num_matches):
            match = chunk_chunk.matches.add()
            match.parent_id = parent_id
            match.score.value = score_value
            match.score.ref_id = chunk_chunk.id
            match.id = 10 * parent_id + score_value
            match.length = 4
    return doc


def test_chunk2doc_ranker_driver_mock_exec():
    doc = create_document_to_score()
    driver = SimpleChunk2DocRankDriver()
    executor = MockLengthRanker()
    driver.attach(executor=executor, pea=None)
    driver._traverse_apply([doc, ])
    assert len(doc.matches) == 4
    # assert int(doc.matches[0].tags['id']) == 70
    assert doc.matches[0].score.value == 7
    # assert int(doc.matches[1].tags['id']) == 60
    assert doc.matches[1].score.value == 6
    # assert int(doc.matches[2].tags['id']) == 50
    assert doc.matches[2].score.value == 5
    # assert int(doc.matches[3].tags['id']) == 40
    assert doc.matches[3].score.value == 4
    for match in doc.matches:
        # match score is computed w.r.t to doc.id
        assert match.score.ref_id == doc.id


def test_chunk2doc_ranker_driver_max_ranker():
    doc = create_document_to_score()
    driver = SimpleChunk2DocRankDriver()
    executor = MaxRanker()
    driver.attach(executor=executor, pea=None)
    driver._traverse_apply([doc, ])
    assert len(doc.matches) == 4
    assert doc.matches[0].id == 70
    assert doc.matches[0].score.value == 7
    assert doc.matches[1].id == 60
    assert doc.matches[1].score.value == 6
    assert doc.matches[2].id == 50
    assert doc.matches[2].score.value == 5
    assert doc.matches[3].id == 40
    assert doc.matches[3].score.value == 4
    for match in doc.matches:
        # match score is computed w.r.t to doc.id
        assert match.score.ref_id == doc.id


def test_chunk2doc_ranker_driver_min_ranker():
    doc = create_document_to_score()
    driver = SimpleChunk2DocRankDriver()
    executor = MinRanker()
    driver.attach(executor=executor, pea=None)
    driver._traverse_apply([doc, ])
    assert len(doc.matches) == 4
    assert doc.matches[0].id == 40
    assert doc.matches[0].score.value == pytest.approx(1 / (1 + 4), 0.0001)
    assert doc.matches[1].id == 50
    assert doc.matches[1].score.value == pytest.approx(1 / (1 + 5), 0.0001)
    assert doc.matches[2].id == 60
    assert doc.matches[2].score.value == pytest.approx(1 / (1 + 6), 0.0001)
    assert doc.matches[3].id == 70
    assert doc.matches[3].score.value == pytest.approx(1 / (1 + 7), 0.0001)
    for match in doc.matches:
        # match score is computed w.r.t to doc.id
        assert match.score.ref_id == doc.id


def test_chunk2doc_ranker_driver_traverse_apply():
    docs = [create_chunk_matches_to_score(), ]
    driver = SimpleChunk2DocRankDriver()
    executor = MinRanker()
    driver.attach(executor=executor, pea=None)
    driver._traverse_apply(docs)
    for doc in docs:
        assert len(doc.matches) == 2
        for idx, m in enumerate(doc.matches):
            # the score should be 1 / (1 + id * 2)
            assert m.score.value == pytest.approx(1. / (1 + m.id * 2.), 0.0001)


def test_chunk2doc_ranker_driver_traverse_apply_larger_range():
    docs = [create_chunk_chunk_matches_to_score(), ]
    driver = SimpleChunk2DocRankDriver(traversal_paths=['cc', 'c'])
    executor = MinRanker()
    driver.attach(executor=executor, pea=None)
    driver._traverse_apply(docs)
    for doc in docs:
        assert len(doc.matches) == 1
        assert len(doc.chunks) == 1
        chunk = doc.chunks[0]
        assert len(chunk.matches) == 2
        min_granularity_2 = chunk.matches[0].score.value
        for idx, m in enumerate(chunk.matches):
            # the score should be 1 / (1 + id * 2)
            if m.score.value < min_granularity_2:
                min_granularity_2 = m.score.value
            assert m.score.value == pytest.approx(1. / (1 + m.id * 2.), 0.0001)
            assert m.score.ref_id == 101
        match = doc.matches[0]
        assert match.score.ref_id == 100
        assert match.score.value == pytest.approx(1. / (1 + min_granularity_2), 0.0001)
