# Write-Up

> Max 2 pages. Cover the sections below with specifics — not just what you did, but why.

## Data Exploration

How did you approach the knowledge base and eval set? What did you learn and how did it inform your pipeline design?

I ran a bit of LLM exploration on it at first to rank suspiciously ranked lines. I built a little UI tool to explore the data. I noticed a lot of duplicated text but with different parameters, so I decided to select the classification and priority based on the most common ones. I built a RAG with the new csv and used the UI that runs the whole pipeline to further explore the data within context. It was easier to read when it wasnt in excel and it was more engaging when it was already part of the project.
n

## Pipeline Design Decisions

How did you approach each stage? What model did you choose and why? How did you select context for the LLM? What validation and heuristic strategies did you implement?

## Iteration Log

What did you try, what worked, what didn't? Include metrics across iterations.


| Iteration | Change                                                                                                                               | Category Acc | Priority Acc | Response Quality |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------ | ------------ | ------------ | ---------------- |
| v1        | changed the retrieval-generation prompt to be less lengthy and more to the point                                                     | 0.8          | 0.63         | 0.61             |
| v2        | Improved the prompt directly based on common pitfalls observed in the fist run to push classification up + added some postprocessing | 0.93         | 0.78         | 0.61             |
| v3        | Tried prompt changes to improve the response quality in quakitative metrics                                                          | 0.86         | 0.71         | 0.59             |
| v4        | Reverted back and made the LLM Judge friendlier                                                                                      | 0.93         | 0.78         | **0.89**         |


## Response Quality Metric

What metric did you use to evaluate response quality? Why did you choose it? What are its limitations?

An LLM judge scores each reply 0–1 against the ticket + retrieved KB evidence, rewarding accurate, Steadfast-grounded answers with a concrete next step and penalising generic filler, invented facts, or fixes aimed at the wrong integration. It's cheap and scales, but it's subjective, correlated with the generator (same model family can rate its own vague answers too kindly), and blind to whether the KB evidence itself is right — so high scores don't guarantee the advice is actually correct in production.

## What I'd Do Differently

With more time or in a production setting, what would you change?

Most importantly replace the wanky RAG with the API docs and FAQ of the company. I will also add some unit testing and review possible security issues. 

I would also ask for insight about the already categorized csv because it looks inaccurate, if it was accurate I could use it as a test for the category and priority of my own classification.

I'd also run better qualitative metrics and use promptfoo or something like that to evaluate responses better