# RCAEval all results

Sample `sample-strat50-ob`, answers sha256 `59c384e42082`. Service-level scores. Intervals are 95%: Wilson score for AC@k and rates; a percentile bootstrap over cases (10000 resamples, seed 3104) for Avg@5, MRR@5 and paired differences.

| Method | n | AC@1 | AC@3 | Avg@5 | MRR@5 | Indicator AC@1 | Abstained | Errors | Median s | p90 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sentinel-healthy-baseline | 50 | 0.780 [0.648, 0.872] | 0.780 [0.648, 0.872] | 0.780 [0.660, 0.880] | 0.780 [0.660, 0.880] | 0.000 [0.000, 0.071] | 2 | 1 | 116.458 | 231.492 |

Paired comparisons: exact McNemar test on AC@1, Holm-adjusted across the rows below, and a paired bootstrap interval for the Avg@5 difference (A minus B).

| A | B | n | Only A correct | Only B correct | McNemar p | Holm p | AC@1 difference | Avg@5 difference |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| _none_ | | | | | | | | |

Notes:

- Unsafe-action rate: Not applicable: RCAEval scores diagnosis only, and no method acts on a cluster.
- sentinel-healthy-baseline returns one service, so AC@3, AC@5 and Avg@5 equal AC@1 by construction.
- Repetitions of the same fault cell are correlated, and n is small: read intervals, not point values, and do not claim one method is better without a paired test.

Deviations:

- sentinel-healthy-baseline: run status FAILED (50/50 predicted, 1 errors)
- sentinel-healthy-baseline: run from a dirty or unknown working tree
