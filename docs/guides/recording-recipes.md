---
description: How to record agent runs for Evidence Judge - Playwright traces and HAR files, browser-use histories, and what to do for Stagehand, Skyvern and other frameworks.
---

# Recording recipes

Evidence Judge reads three formats. Anything that can produce one of them can be judged.

| Format | Flag | Channels |
| --- | --- | --- |
| Playwright trace (v7–v10) | `--trace` | actions, network, snapshots, screenshots, console, final page |
| HAR 1.2 | `--har` | network |
| browser-use `AgentHistoryList` JSON | `--browser-use` | actions, screenshots |

The richer the recording, the more claims can be decided. Network evidence matters most: it is
what turns *"I placed the order"* into a confident yes or no.

## Playwright (Python)

=== "Sync"

    ```python
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(record_har_path="run/run.har")
        context.tracing.start(screenshots=True, snapshots=True)
        page = context.new_page()
        answer = my_agent(page)
        context.tracing.stop(path="run/trace.zip")
        context.close()
        browser.close()
    ```

=== "Async"

    ```python
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(record_har_path="run/run.har")
        await context.tracing.start(screenshots=True, snapshots=True)
        page = await context.new_page()
        answer = await my_agent(page)
        await context.tracing.stop(path="run/trace.zip")
        await context.close()
        await browser.close()
    ```

=== "Node / TypeScript"

    ```ts
    import { chromium } from "playwright";

    const browser = await chromium.launch();
    const context = await browser.newContext({ recordHar: { path: "run/run.har" } });
    await context.tracing.start({ screenshots: true, snapshots: true });
    const page = await context.newPage();
    const answer = await myAgent(page);
    await context.tracing.stop({ path: "run/trace.zip" });
    await context.close();
    await browser.close();
    ```

Then:

```bash
evidence-judge judge --trace run/trace.zip --har run/run.har \
  --answer "$ANSWER" --spec spec.yaml
```

!!! tip "Keep sensitive pixels out"

    Trace screencast frames cannot be masked. On real accounts, hide sensitive elements with
    injected CSS (`page.add_style_tag`) or record with `tracing.start(screenshots=False)`;
    snapshots and network evidence are what most checks use anyway.

## browser-use

browser-use histories are read natively: actions (navigate, click, input, send keys, select,
upload), the agent's reasoning (thinking, evaluation, memory, next goal), its final `done`
answer, and screenshots. Save the history after the run:

```python
from browser_use import Agent, Browser

browser = Browser(record_har_path="run/run.har")  # optional, adds the network channel
agent = Agent(task="Buy a red kettle under $30", llm=llm, browser=browser)
history = await agent.run()
history.save_to_file("run/history.json")
```

```bash
evidence-judge judge --browser-use run/history.json --har run/run.har --spec spec.yaml
```

`--browser-use` also accepts a run directory containing `history.json`. The answer and reasoning
come from the history, so `--answer-file` is optional (it overrides the history's answer).

!!! warning "Without a HAR, there is no network channel"

    A history alone records actions and screenshots, not requests. Claims like *"I placed the
    order"* can then only be left unverified, and the run goes to `NEEDS_REVIEW`. Record a HAR
    (`record_har_path` on the browser) to make them decidable.

Screenshots referenced by `screenshot_path` are read only from inside the history's directory
(symlinks resolved), must be real PNG, JPEG, WebP or GIF files of at most 10 MB, and are skipped
with a warning otherwise. Widen the allowed directory with `--screenshot-root DIR`.

## Stagehand, Skyvern and other frameworks

There is **no native adapter** for Stagehand, Skyvern, or OpenAI / Anthropic computer use yet;
those are on the [roadmap](https://github.com/hsayvaidya23/evidence-judge#roadmap). What works
today is any recording in a supported format:

- If the framework gives you access to the Playwright `BrowserContext` it drives, start tracing
  and HAR recording on it as above.
- If it drives a Chromium browser you can launch yourself, a HAR of that browser's traffic gives
  Evidence Judge the network channel, which is enough to check most write tasks
  (`--har run.har --answer-file answer.txt --spec spec.yaml`).
- Save the agent's final answer to a file and pass it with `--answer-file`.

If you already have structured results, `--claims claims.json` skips extraction and lets you
supply the claims and predicates yourself; see
[Claims & predicates](../concepts/claims-and-predicates.md).

## Check before you judge

```bash
evidence-judge inspect --trace run/trace.zip --har run/run.har
evidence-judge inspect --browser-use run/ --json
```

`inspect` lists the captured channels, request counts, state-changing requests and the final URL,
redacted, without judging anything.
