Prompt eval: real/sonnet @ 25a5aa9f3 (1 rep) (no earlier run to compare with)

| task | success | steps | parallel Workers | retries | reviews | gates | room messages | room deliveries | room inbox calls | empty room inbox calls | room inbox calls woken by delivery | contract violations | Planner file edits | tokens (prompt / output) | cost (USD) | first-pass rate | attempts per passing case | cost per passing case (USD) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| single_step | 1/1 | 1.0 | – | 0.0 | 0.0 | – | – | – | – | – | – | 0.0 | 0.0 | – | 0.0939 | – | – | – |
| dependent_steps | 1/1 | 2.0 | – | 0.0 | 0.0 | – | – | – | – | – | – | 0.0 | 0.0 | – | 0.1473 | – | – | – |
| reviewed_step | 1/1 | 1.0 | – | 0.0 | 1.0 | – | – | – | – | – | – | 0.0 | 0.0 | – | 0.1309 | – | – | – |
| retry_after_failure | 1/1 | 1.0 | – | 1.0 | 0.0 | – | – | – | – | – | – | 0.0 | 0.0 | – | 0.1428 | – | – | – |
| chat_correction | 1/1 | 1.0 | – | 0.0 | 0.0 | – | – | – | – | – | – | 0.0 | 0.0 | – | 0.1083 | – | – | – |
| **total** | 5/5 (100%, 57–100%) | 6.0 | – | 1.0 | 1.0 | – | – | – | – | – | – | 0.0 | 0.0 | – | 0.6232 | – | – | – |

- the new run ran each case once: a pass or fail per case is a single sample
