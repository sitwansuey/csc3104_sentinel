# RCAEval eval results

Sample `sample-ob-v1`, answers sha256 `aaaaaaaaaaaa`. Service-level scores. Intervals are 95%: Wilson score for AC@k and rates; a percentile bootstrap over cases (1000 resamples, seed 11) for Avg@5, MRR@5 and paired differences.

| Method | n | AC@1 | AC@3 | Avg@5 | MRR@5 | Indicator AC@1 | Abstained | Errors | Median s | p90 s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| anomaly-rank | 10 | 0.400 [0.168, 0.687] | 0.600 [0.313, 0.832] | 0.600 [0.300, 0.860] | 0.525 [0.250, 0.800] | 0.300 [0.108, 0.603] | 0 | 0 | 0.400 | 0.510 |
| baro | 10 | 0.500 [0.237, 0.763] | 0.800 [0.490, 0.943] | 0.720 [0.460, 0.920] | 0.633 [0.367, 0.850] | 0.300 [0.108, 0.603] | 0 | 0 | 0.500 | 0.610 |
| sentinel | 10 | 0.400 [0.168, 0.687] | 0.400 [0.168, 0.687] | 0.400 [0.100, 0.700] | 0.400 [0.100, 0.700] | 0.000 [0.000, 0.278] | 1 | 1 | 107.500 | 186.000 |

Paired comparisons: exact McNemar test on AC@1, Holm-adjusted across the rows below, and a paired bootstrap interval for the Avg@5 difference (A minus B).

| A | B | n | Only A correct | Only B correct | McNemar p | Holm p | AC@1 difference | Avg@5 difference |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| anomaly-rank | baro | 10 | 1 | 2 | 1.000 | 1.000 | -0.100 | -0.120 [-0.300, 0.020] |
| anomaly-rank | sentinel | 10 | 1 | 1 | 1.000 | 1.000 | 0.000 | 0.200 [-0.020, 0.480] |
| baro | sentinel | 10 | 1 | 0 | 1.000 | 1.000 | 0.100 | 0.320 [0.080, 0.600] |

Notes:

- Unsafe-action rate: Not applicable: RCAEval scores diagnosis only, and no method acts on a cluster.
- sentinel returns one service, so AC@3, AC@5 and Avg@5 equal AC@1 by construction.
- Repetitions of the same fault cell are correlated, and n is small: read intervals, not point values, and do not claim one method is better without a paired test.

Deviations:

- none found in the result files
