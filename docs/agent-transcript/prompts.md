# Prompts given to the coding agent

Every instruction given to Claude Code during this exercise, in order and
verbatim. Slash commands, tool results and system messages are omitted.
The agent's replies are in `conversation.md`.

**12 prompts.** Email addresses and home directory paths are redacted.

---

## Prompt 1

```text
https://github.com/infinitelambda/flowline-pwc-demo lets clone that repo and create a local copy for ourselves to edit
```

## Prompt 2

```text
drop the remote and repoint it at a new repo in my github
```

## Prompt 3

```text
done, squash to a fresh initial commit
```

## Prompt 4

```text
So this is the assignment:
```

## Prompt 5

```text
Ok so now, the assignment is:
Take-home exercise: Informatica PowerCenter to dbt converter

This exercise is part of our interview process for the Product Engineer role.



<pasted_content id="e44c">
What we are asking

Build a small tool that converts an Informatica PowerCenter mapping into an equivalent dbt model. We have attached one sample PowerCenter mapping file in XML to work from. Download the demo XML workflow here: https://github.com/infinitelambda/flowline-pwc-demo

The mapping mirrors the dbt jaffle shop demo project, which is public. You can use that project as a reference target: run it, look at its models, and check your converter's output against it. Build a converter that derives the dbt model from the XML. Do not hard-code the jaffle shop output, because we are assessing the conversion logic, not the final SQL.
</pasted_content id="e44c">


We already cloned the repo locally and created one ourselves.



<pasted_content id="e44c">
Background

Informatica PowerCenter is an ETL tool. A mapping describes how data moves from source tables to target tables through a chain of transformations (filters, joins, lookups, aggregations, and so on). Mappings can be exported as XML, which is the format we have given you.





Mappings overview: https://docs.informatica.com/data-integration/powercenter/10-5/designer-guide/mappings/mappings-overview.html



Transformation guide: https://docs.informatica.com/data-integration/powercenter/10-5/transformation-guide/preface.html

dbt transforms data already in a warehouse using SQL. A dbt model is a single SQL select statement in a .sql file that dbt turns into a table or view. Models can reference each other, and you can attach tests and documentation.





What is dbt: https://docs.getdbt.com/docs/introduction



About dbt models: https://docs.getdbt.com/docs/build/models



SQL models: https://docs.getdbt.com/docs/build/sql-models



Quickstart guides: https://docs.getdbt.com/guides



Jaffle shop reference project (runs locally with DuckDB in about a minute): https://github.com/dbt-labs/jaffle_shop_duckdb

The task





Read the sample PowerCenter XML and work out what the mapping does.



Build a converter, in any language you choose, that reads a PowerCenter mapping XML and produces an equivalent dbt model.



Include tests. You can check your output against the jaffle shop reference project.



Handle what you reasonably can in the time. We care more about how you reason about the problem than about full coverage.
</pasted_content id="e44c">




<pasted_content id="e44c">
What to submit

A GitHub repository (public, or private with access granted to [GitHub handles]) or a zipped folder, with a short README that covers:





How to run it.



What you assumed, and what you chose not to handle.



Where the converter is most likely to produce wrong output.



Where a coding agent got something wrong, and how you caught it.



The prompts you gave the coding agent, starting with your initial prompt. If your tool can export the full agent conversation, include it as a file in the repository.



A short outline of the process you followed with the agent to build the solution.
</pasted_content id="e44c">




<pasted_content id="e44c">
What we are looking for





You understand the code you submit and can explain any part of it.



Sensible handling of the conversion, with a clear view of where it breaks.



Clear reasoning in the README.
</pasted_content id="e44c">


Keep the part in mind about me understanding the code I submit and explaining every part of it, I want you to explain things to me short and concise so I can grasp what's going on.
Keep in mind I haven't used this stack before, I've only used python from the stack we are going with.

We will write the implementation in Python.

Let's first plan the work, ask me any questions before starting, do the research, check if there are MCPs or skills which already exist with the stack and we can use in order to follow the best practices and coding conventions.
Please explain to me also in short and concise words what we are going for.

The XML file is quite huge so I haven't went through it, I understand it's content, but don't have time and reason to go through it line by line. Main parts - me understanding the problem, the stack, and the solution.
```

## Prompt 6

```text
# TODO: move validations into a separate model specific for validating the input
I think the function is gonna be more readable that way

also I noticed most of the comments in code ("documentation") are quite hard to understand or give no value, can we please leave comments only where necessary and code is not self explanatory, also for me the terminology of different thigns is hard to understand such as in the models - mapping, instance, source vs target etc, i have good knowledge in DBs, just in this case it's hard to understand what each one is gonna be used for
```

## Prompt 7

```text
let's open a PR for the current phase, we will do that on each phase, while I review the code you start working on next phase,
  but before that ofc ask me any clarifying questions or anything u need from me, good to manual test our code as well when possible so I can see how it works in practice, because automated tests are testing in isolation and could not always show the actual result, also ensure tests are testing the behavior and not just code, remember this for our next automated tests as well, we must test the behavior

write that down about the price as sents and vice versa in the PR's curious stuff in the description, so I have it noted there as well
```

## Prompt 8

```text
stacking is fine, ill do a manual test now and do code review
```

## Prompt 9

```text
i merged chunk 1, reviewing chunk 2 now, feel free to start working on chunk 3, please rebase with latest main chunk 2 if needed since I merged, i havent made changes there
```

## Prompt 10

```text
how many chunks did we decide to split this into? im thinking of changing the strategy cuz reading the code for each chunk takes too much time and I just starts understanding the code and get piled on with new code to read which is overwhelming, I think what we should do instead is do the chunks we know need to be done - u do them.
Then after u are done, we test everything, I read through the code, give you my suggestions on what to fix, you explain to me stuff I don't understand and voala, we should be good
```

## Prompt 11

```text
merge them all into main, I'll test after and do code review from main
```

## Prompt 12

```text
okay so good progress so far, but how can I test the before and after myself manually e2e, meaning - i want to see how powercenter was behaving with data and then how DBT is visualising and behaving with the data after it goes through the converter, do i need to install any apps or anything so I can check this?
```
