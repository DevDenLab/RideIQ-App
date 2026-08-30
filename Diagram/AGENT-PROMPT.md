# Agent prompt — generate the RideIQ diagram images

Hand the block below to a browser-capable agent (Claude in Chrome, or any agent that can
drive a real browser session). It generates one image per diagram spec in this folder.

Everything it needs to paste is pre-flattened into `Diagram/prompts/*.prompt.txt`, so the
agent never has to parse JSON in the browser.

---

## The prompt

> **Task: generate 16 diagram images for the RideIQ project using Google Gemini's image
> generation (Nano Banana Pro), driving my browser.**
>
> **Source files.** On this machine:
> `C:\Users\tatva\OneDrive\Desktop\RideIQ App\Diagram\prompts\`
> That folder holds 16 `.prompt.txt` files, numbered `01`–`16`. Each file is one complete,
> ready-to-paste prompt — do not edit, summarise, reformat or shorten the contents. Paste
> each file verbatim.
>
> **Save the results to:**
> `C:\Users\tatva\OneDrive\Desktop\RideIQ App\Diagram\generated\`
> Create that folder if it does not exist. Name each image after its source file, so
> `01-system-architecture.prompt.txt` produces `01-system-architecture.png`.
>
> **Before you start**, read `Diagram\prompts\_MANIFEST.tsv`. It lists every file with its
> title and required aspect ratio (some are `16:9`, some are `4:3`). Use the ratio given for
> each file, not one ratio for all of them.
>
> ### Steps, repeated once per file, in numerical order
>
> 1. Open a new Gemini chat at `https://gemini.google.com`. **Use a fresh chat for each
>    diagram** — carrying context between them makes later images inherit earlier layouts.
> 2. Turn on image generation and select the **Nano Banana Pro** image model. The control's
>    exact name and position change between Gemini releases, so look at the page and find
>    it rather than assuming a fixed location — it is usually a model picker near the prompt
>    box, or a "Create images" / tools affordance. If you genuinely cannot find a Nano
>    Banana Pro option, stop and tell me what the UI offers instead. Do not silently fall
>    back to a different model.
> 3. Read the `.prompt.txt` file from disk and paste its **entire** contents into the prompt
>    box. These are long (3–9 KB); that is intended. Append one line:
>    `Aspect ratio: <ratio from the manifest>.`
> 4. Submit and wait for the image to finish rendering.
> 5. **Check the result before accepting it.** Reject and regenerate if: text is garbled or
>    misspelled, labels overlap or are unreadable, a component named in the prompt is
>    missing, or the model invented components that were not asked for. Up to **3 attempts**
>    per diagram, then keep the best one and note the problem.
> 6. Save the chosen image to the output folder with the name above.
> 7. Move to the next file.
>
> ### Rules
>
> - **Ask me before downloading anything.** Tell me the filename and where it is going, and
>   wait for a yes. One approval covering the whole batch is fine — just ask once, up front.
> - **Do not enter credentials.** Use the browser session that is already signed in. If
>   Gemini asks me to log in, stop and tell me.
> - **Do not accept cookie or consent banners on my behalf** beyond declining non-essential
>   ones.
> - If Gemini refuses a prompt or rate-limits you, stop and report which file and what it
>   said. Do not rewrite the prompt to get around a refusal.
>
> ### Report back
>
> A table with one row per diagram: file, attempts used, saved / failed, and a one-line note
> on anything that came out wrong. Flag specifically any image where the **text** is wrong,
> since these diagrams are mostly labels and a pretty picture with garbled labels is
> useless.
>
> ### Two that are worth extra care
>
> - `09-seq-bluegreen-deploy` and `13-algo-ml-map` are dense with text. If neither comes out
>   clean in 3 attempts, say so plainly — those two are better produced as Mermaid diagrams
>   than as generated images, and I would rather know than receive a blurry one.

---

## Why the prompts are pre-flattened

Each `.prompt.txt` already contains, in order:

1. the `image_prompt` from its JSON — the visual instruction,
2. the aspect ratio and style,
3. the full `spec` object, introduced as *"follow this structure exactly"*,
4. a short list of legibility requirements.

If a diagram comes out consistently poor, try pasting **only** the first paragraph
(the `image_prompt`). Image models sometimes do better with less structural detail, at the
cost of label accuracy.

## Regenerating the prompt files

They are derived from the `NN-*.json` files in this folder. If you edit a JSON, rebuild the
prompts rather than hand-editing the `.txt`.
