import numpy as np

from evaluation.diversity import quality


def test_coverage_rewards_topic_coverage_not_duplicate_representatives():
    vectors = {
        "solar": np.array([1.0, 0.0]),
        "solar-copy": np.array([1.0, 0.0]),
        "wind": np.array([0.0, 1.0]),
    }
    reference = {"source": [
        ("heldout-solar", np.array([1.0, 0.0])),
        ("heldout-wind", np.array([0.0, 1.0])),
    ]}
    centroids = {"source": np.array([1.0, 1.0]) / np.sqrt(2)}
    docs = [
        {"id": identity, "source_key": "source", "title": title}
        for identity, title in (
            ("solar", "Solar energy"), ("solar-copy", "Solar energy"),
            ("wind", "Wind turbines"),
        )
    ]
    repeated = quality(docs[:2], vectors, reference, centroids)
    varied = quality([docs[0], docs[2]], vectors, reference, centroids)
    assert varied["coverage"] > repeated["coverage"]
    assert varied["semantic_redundancy"] < repeated["semantic_redundancy"]
    assert varied["lexical_redundancy"] < repeated["lexical_redundancy"]
    assert varied["centrality"] == repeated["centrality"]
