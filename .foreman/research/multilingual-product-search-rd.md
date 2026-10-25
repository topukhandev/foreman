<!-- Foreman · task #21 · 2026-10-25T15:42 -->

# Research Brief: Multilingual Product Search Dataset & Foreman R&D Evaluation

## 1. Question
Can Foreman coordinate multi-provider AI sessions in R&D mode to research, design, acquire, validate, and deliver a realistic multilingual (Bengali and English) product search dataset and benchmark, and how does Foreman perform against its architectural responsibilities?

---

## 2. Key Findings

### 2.1 Foreman Orchestrator Capabilities and Operational Constraints
- **Execution Flow & Lifecycle**:
  Foreman operates as a single-process orchestration loop defined in [`src/orchestrator/engine.py:440-490`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L440-L490). The sequence runs:
  `_plan` ([`engine.py:500-540`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L500-L540)) $\rightarrow$ optional impact investigation (`_ensure_impact`, [:621](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L621)) $\rightarrow$ parallel step execution via `ThreadPoolExecutor` (`_run_steps`, [:666-695](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L666-L695)) $\rightarrow$ verification (`_verify`, [:472](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L472), only if implement steps ran) $\rightarrow$ documentation (`_document`, [:1009](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L1009)) $\rightarrow$ finalization (`_finalize`, [:1045](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L1045)). Plan expansions on discovered dependencies are capped by `max_replans = 2` ([`src/orchestrator/config.py:55`](file:///var/www/projects/orchestrator/src/orchestrator/config.py#L55)).
- **R&D Mode Restrictions**:
  Under [`modes/research.md:1-28`](file:///var/www/projects/orchestrator/modes/research.md#L1-L28), R&D mode sets `writes: none`, `web: true`, `checks: []`, `document: research`, `next: planning`. The planner is constrained to at most 3 investigate steps, and any implement step is forcibly rewritten to investigate ([`engine.py:591`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L591)). The engine produces exactly one output document saved to `.foreman/research/<topic>.md` in the home project ([`engine.py:1037`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L1037)). Consequently, Foreman cannot generate multi-file dataset deliverables (`products.jsonl`, `benchmark-queries.jsonl`, `validation/`) in R&D mode; physical generation requires an authorized transition to development mode.
- **Enforced Read-Only and Tool Sandboxing**:
  Claude workers are restricted to `READ_TOOLS` (`Read`, `Grep`, `Glob`, `WebSearch`, `WebFetch`, and an allowlist of `git`/`ls`/`cat`/`find`/`grep`), with `Edit`, `Write`, and `NotebookEdit` disallowed ([`src/orchestrator/providers/claude.py:13-17`](file:///var/www/projects/orchestrator/src/orchestrator/providers/claude.py#L13-L17)). Antigravity runs with `--mode=plan` and `--dangerously-skip-permissions` ([`src/orchestrator/providers/antigravity.py:6-11, 53-60`](file:///var/www/projects/orchestrator/src/orchestrator/providers/antigravity.py#L6-L11)), where terminal commands are denied ([`providers/antigravity.py:24-26`](file:///var/www/projects/orchestrator/src/orchestrator/providers/antigravity.py#L24-L26)). Workers cannot execute `curl`, Python extraction scripts, or automated network checks in R&D mode. Tree diffs run after every step to detect illicit modifications ([`engine.py:767-770`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L767-L770)), and any HEAD movement is flagged as a policy violation ([`engine.py:765-766`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L765-L766)).
- **Provider Scheduling and Diversity Gaps**:
  The selection algorithm `_choose` ([`engine.py:795-825`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L795-L825)) prioritizes web capability for research steps ([:801-802](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L801-L802)), step session continuation ([:803-806](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L803-L806)), task session continuation for the same project ([:808-812](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L808-L812)), session forking ([:813-824](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L813-L824), Claude/OpenCode only; Antigravity lacks fork), and fallback to `avail[0]`. Because session continuity is favored, all steps in a single project default to the same provider. True multi-provider diversity only triggers upon failure fallback or during `_review` ([`engine.py:970`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L970), `_brain(prefer_not=implementers)`).
- **Concurrency & Project Serialization**:
  Parallelism is bounded to one active step per project ([`engine.py:671, 680`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L671)). If all investigative steps target `orchestrator`, they execute serially regardless of `max_parallel=4`.
- **Context Truncation**:
  Prompts assemble task instructions, `.foreman` context, up to 8 prior step findings, and decisions ([`engine.py:827-872`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L827-L872)). However, findings are truncated (step summaries to 300 characters, up to 8 findings to 200 characters each; [:833-836](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L833-L836)), causing detailed analytical tables to degrade across dependencies.
- **Traceability and Telemetry Metrics**:
  Foreman records execution artifacts in `~/.foreman/tasks/<id>/` (`task.json`, `events.jsonl`, `logs/step-<id>-<provider>.txt`, `report.md`). Event lifecycles (`created`, `plan`, `step_start`, `step_done`, `finished`) are inspectable via `foreman show <id> --events`. While `RunResult` defines `duration` and `cost_usd` ([`src/orchestrator/providers/base.py:57-58`](file:///var/www/projects/orchestrator/src/orchestrator/providers/base.py#L57-L58)), only Claude populates cost ([`providers/claude.py:93`](file:///var/www/projects/orchestrator/src/orchestrator/providers/claude.py#L93)), and `engine.py` does not persist duration, cost, or tokens to `step_done` events.

### 2.2 Data Sources & Legal/Licensing Evaluation
- **Open Food Facts (OFF)**:
  - *Licensing*: Database under ODbL 1.0, individual contents under DbCL 1.0, product images under CC BY-SA 3.0 ([OFF Legal Guide](https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/tutorials/license-be-on-the-legal-side/)). Requires attribution ("Contains data from Open Food Facts, available under the Open Database License") and Share-Alike on derivative databases ([OFF Terms](https://world.openfoodfacts.org/terms-of-use)).
  - *Availability*: Daily bulk exports in JSONL, TSV, and Parquet on Hugging Face (4.91M records total; [`openfoodfacts/product-database`](https://huggingface.co/datasets/openfoodfacts/product-database)).
  - *Coverage*: 1,336 products tagged for Bangladesh ([bd-en.openfoodfacts.org](https://bd-en.openfoodfacts.org/)). Barcode (`code`) provides verified GTIN identifiers.
  - *Limitations*: Narrow category focus (food, grocery, beauty); no pricing attributes; Bengali-script (`bn`) coverage is unverified in primary metadata and requires post-download filtering.
- **Amazon Berkeley Objects (ABO)**:
  - *Licensing*: CC BY 4.0 ([Registry of Open Data on AWS](https://registry.opendata.aws/amazon-berkeley-objects/)). Permits commercial redistribution and modification with attribution to Amazon.com and named researchers.
  - *Availability*: 147,702 non-food products across home goods, furniture, electronics, and apparel; 398,212 catalog images.
  - *Limitations*: Contains no transactional prices or stock availability. Language breakdown is unverified (requires local parsing of `listings/README.md`); Indic language representation is assumed negligible.
- **Amazon Shopping Queries Dataset (ESCI)**:
  - *Licensing*: Code and dataset metadata under Apache-2.0 ([github.com/amazon-science/esci-data](https://github.com/amazon-science/esci-data)).
  - *Coverage*: 48,300 queries (reduced) and 130,652 queries (full) across English, Spanish, and Japanese. Covers 16 query failure modes.
  - *Fit*: Not a source of product catalog records or Bengali queries, but serves as the standard template for product search relevance judgments.
- **Daraz Bangladesh Product Datasets & Live Web Crawling**:
  - *Mendeley Dataset*: "Daraz BD Products & Reviews" (July 2026, [doi:10.17632/k5b6vxv25r.1](https://data.mendeley.com/datasets/k5b6vxv25r/1)) is licensed under CC BY-NC 4.0 and includes explicit reservations prohibiting text and data mining (TDM) and commercial AI training. This violates the confirmed mandate to use only permissive open licenses (CC0, CC-BY, ODbL).
  - *Live Scraping*: `https://www.daraz.com.bd/robots.txt` explicitly disallows `/catalog/` and `/shop/*.htm` for all web agents. Scraping Daraz directly is prohibited.
- **Supplementary Knowledge Bases**:
  - *Wikidata Labels*: Parquet dump `rayliuca/WikidataLabels` under CC0-1.0 provides 9.77M Bengali entity labels ([Hugging Face](https://huggingface.co/datasets/rayliuca/WikidataLabels)), suitable for generating validated English-Bengali brand and category alias mappings.
  - *Aksharantar (AI4Bharat)*: Open benchmark (~26M pairs across 21 Indic languages; [arXiv:2205.03018](https://arxiv.org/abs/2205.03018)) supporting Roman-to-Indic transliteration. While primarily designed for Roman $\rightarrow$ Bengali, it provides phonetic mappings to construct synthetic Banglish query variants from Bengali titles.

### 2.3 Critical Gap: The Bengali-Script Catalog Void
No permissively licensed (CC0, CC-BY, ODbL) e-commerce catalog with substantial Bengali-script product titles and descriptions currently exists in public repositories (Hugging Face, Kaggle, Mendeley, or Zenodo). E-commerce datasets from Bangladesh (e.g., DarazMM, EComReviews-BD) consist exclusively of customer sentiment reviews, synthetic intent classifications (BanglaEComIntent, CC BY-NC-SA 4.0), or non-redistributable proprietary crawls. Consequently:
1. Bengali-script product records must be derived by augmenting open sources (OFF Bangladesh subset and ABO) with verified translations, brand aliases, and transliterations.
2. Observed catalog text must remain byte-verbatim, while all translations and synthetic aliases must be segregated into explicit `derived` fields with transparent provenance.

---

## 3. Existing Solutions and Options Compared

### 3.1 Primary Data Source Strategy

| Option | Pros | Cons | Licensing & Rights | Fit for Project |
|---|---|---|---|---|
| **Option A: Open Food Facts (OFF) [Bangladesh + Global]** | Verified GTIN barcodes; authentic FMCG products; native `product_name` language arrays; verified bulk exports. | Category limited to food/beauty; no pricing data; uncertain native Bengali title count. | ODbL 1.0 (DbCL contents, CC BY-SA 3.0 images); requires Share-Alike attribution. | **High (Primary FMCG)**: Supplies verifiable real-world products and international barcodes. |
| **Option B: Amazon Berkeley Objects (ABO)** | 147k records; broad category diversity (electronics, furniture, apparel); rich physical attributes; high-res images. | Zero pricing information; zero native Bengali text; large media footprint requires selective metadata extraction. | CC BY 4.0; fully permissive commercial redistribution with attribution. | **High (Primary Durable Goods)**: Solves category diversity and multi-attribute search requirements. |
| **Option C: Daraz BD Mendeley Dataset** | Authentic Bangladeshi e-commerce catalog; local product selection; Bengali and English mixed titles. | Non-commercial restriction (CC BY-NC 4.0); explicit TDM/AI training reservation; third-party seller image copyrights. | CC BY-NC 4.0 + TDM reservation; incompatible with open redistribution rules. | **Reject**: Violates the confirmed project decision requiring open licenses (CC0, CC-BY, ODbL). |
| **Option D: Direct Web Crawling (Daraz, Chaldal, Rokomari)** | Hyper-current pricing, availability, and authentic Bengali catalog listings. | `robots.txt` disallow rules; anti-bot blocks; Terms of Service violations; fragile and non-reproducible pipeline. | Proprietary catalog copyrights; unpermitted crawling exposes legal liability. | **Reject**: Violates Foreman R&D safety rules and web crawling policies. |
| **Option E: Composite Open Pipeline (OFF + ABO + Wikidata)** | Strict license compliance; category balance; verifiable barcodes; programmatic alias enrichment. | Multi-license composition (ODbL + CC BY) requires field-level license tagging. | ODbL + CC BY 4.0 + CC0 1.0 maintained via strict record-level provenance. | **Selected Strategy**: Best balance of legal compliance, authenticity, and attribute coverage. |

### 3.2 Orchestration Execution Models

| Execution Path | Strengths | Weaknesses | Architectural Feasibility |
|---|---|---|---|
| **Foreman R&D Mode (Current)** | Read-only safety guarantees; automatic provider session management; strict git protection. | Cannot write JSONL/benchmark files; cannot run shell/python data collectors; no verification/review phase. | Feasible for research brief and schema design; blocked for physical dataset delivery. |
| **Foreman Development Mode (Follow-up)** | Permits workspace writes (`mode: write`); executes check commands; runs automated validation and independent reviews. | Requires explicit user authorization; expands write surface; must maintain strict test boundaries. | Mandatory next step for Stage D (Acquisition) through Stage G (Verification). |
| **Manual / Ad-Hoc Pipeline Execution** | Bypasses all orchestrator tool restrictions; rapid script iteration. | Violates Operating Rule #2 ("Do not bypass Foreman"); destroys workflow evaluation and traceability. | **Disallowed**: Defeats the primary objective of benchmarking Foreman. |

---

## 4. Pain Points and Opportunities

### 4.1 Foreman Orchestration Gaps
- **The "Read-Only Write Paradox"**: The user request demands physical dataset files (`products.jsonl`, `benchmark-queries.jsonl`, `validation/`), but Foreman's R&D mode enforces `writes: none` ([`modes/research.md:4`](file:///var/www/projects/orchestrator/modes/research.md#L4)) and rewrites implement steps to investigate ([`engine.py:591`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L591)). R&D workers cannot invoke compilers, interpreters, or download tools.
- **Provider Monoculture via Session Continuity**: The heuristic in `_choose` ([`engine.py:808-812`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L808-L812)) binds consecutive steps in the same project to the initial provider session. Multi-provider division of labor (e.g., Claude on licensing, Antigravity on schema) does not occur naturally unless forced across distinct project boundaries or explicit failures.
- **Silent Context Degradation**: Passing prior findings as truncated snippets (200 characters across 8 items; [`engine.py:833-836`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L833-L836)) discards complex structured schema definitions and URL references across sequential steps.
- **Omission of Verification in Research Mode**: Because `_verify` and `_review` trigger exclusively when `implement` steps exist ([`engine.py:472-476`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L472-L476)), research findings are never subjected to independent peer review or automated assertion checking.
- **Prompt Token Bloat**: The orchestrator injects the raw user prompt verbatim into every worker, impact, planning, and documentation call ([`engine.py:857`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L857)), compounding token consumption without semantic summarization.

### 4.2 Linguistic and Data Integrity Challenges
- **Bengali Unicode Orthography**: Bengali search matching fails under naive string comparisons due to Unicode decomposition ambiguities:
  - Khanda-ta representation: atomic U+09CE (`ৎ`) vs. decomposed U+09A4 U+09CD (`ত্`).
  - Ya-phala representation: U+09CD U+09AF (`্য`) with or without Zero Width Joiners/Non-Joiners (`ZWJ` U+200D, `ZWNJ` U+200C).
  - Nukta compositions: atomic characters (e.g., U+09DC `ড়`) vs. base character + nukta (U+09A1 U+09BC `ড`+`়`).
  - Strict Unicode Normalization Form C (NFC) and custom character folding are required during search indexing.
- **Banglish (Transliteration) Entropy**: Phonetic romanization lacks standard spelling rules (e.g., Bengali "শাড়ি" can be transliterated as *sharee*, *shari*, *saari*, or *saree*). Deterministic rule-based transliteration engines fail to capture colloquial user query habits.
- **Dual License Contamination**: Open Food Facts enforces ODbL Share-Alike on derivative databases, while ABO operates under CC BY 4.0. Distributing a single monolithic dataset without record-level provenance risks license invalidation.

---

## 5. Technical Specification & Implementation Plan

### 5.1 Dataset Schema Architecture (`dataset-schema.json`)
The schema implements a layered JSONL design adhering to schema.org Product standards, strictly isolating raw observed source data from synthetic normalizations.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "MultilingualProductRecord",
  "type": "object",
  "required": ["identity", "observed", "provenance"],
  "properties": {
    "identity": {
      "type": "object",
      "required": ["id", "source", "source_id"],
      "properties": {
        "id": { "type": "string", "description": "Deterministic SHA-256 hash of source + source_id" },
        "source": { "type": "string", "enum": ["open_food_facts", "amazon_berkeley_objects", "wikidata"] },
        "source_id": { "type": "string" },
        "canonical_url": { "type": "string", "format": "uri" },
        "gtin": { "type": "string", "pattern": "^[0-9]{8,14}$", "description": "Normalized GTIN-8/12/13/14 with verified check digit" },
        "sku": { "type": "string" },
        "mpn": { "type": "string" },
        "parent_id": { "type": "string" },
        "variant_axes": { "type": "object", "additionalProperties": { "type": "string" } }
      }
    },
    "observed": {
      "type": "object",
      "required": ["titles"],
      "properties": {
        "titles": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["text", "lang", "script"],
            "properties": {
              "text": { "type": "string" },
              "lang": { "type": "string" },
              "script": { "type": "string", "enum": ["bn", "en", "bl", "mx"] },
              "source_field": { "type": "string" }
            }
          }
        },
        "descriptions": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": { "text": { "type": "string" }, "lang": { "type": "string" }, "source_field": { "type": "string" } }
          }
        },
        "brand": { "type": "string" },
        "categories_raw": { "type": "array", "items": { "type": "string" } },
        "attributes": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["key", "value"],
            "properties": { "key": { "type": "string" }, "value": { "type": "string" }, "unit": { "type": "string" } }
          }
        },
        "price": {
          "type": "object",
          "properties": {
            "amount": { "type": "number" },
            "currency": { "type": "string", "pattern": "^[A-Z]{3}$" },
            "collected_at": { "type": "string", "format": "date-time" }
          }
        },
        "images": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": { "url": { "type": "string" }, "license": { "type": "string" } }
          }
        }
      }
    },
    "derived": {
      "type": "object",
      "properties": {
        "title_norm": { "type": "string", "description": "NFC normalized, trimmed, lowercased" },
        "category_std": { "type": "string" },
        "brand_aliases": {
          "type": "array",
          "items": { "type": "object", "properties": { "text": { "type": "string" }, "script": { "type": "string" }, "source": { "type": "string" } } }
        },
        "title_translations": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["text", "lang", "method", "model_version"],
            "properties": {
              "text": { "type": "string" },
              "lang": { "type": "string" },
              "method": { "type": "string" },
              "model_version": { "type": "string" },
              "confidence": { "type": "number" }
            }
          }
        },
        "title_transliterations": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["text", "script", "scheme"],
            "properties": {
              "text": { "type": "string" },
              "script": { "type": "string", "enum": ["bl", "bn"] },
              "scheme": { "type": "string" },
              "method": { "type": "string" }
            }
          }
        }
      }
    },
    "provenance": {
      "type": "object",
      "required": ["source_dataset", "license_id", "retrieved_at"],
      "properties": {
        "source_dataset": { "type": "string" },
        "source_url": { "type": "string" },
        "retrieved_at": { "type": "string", "format": "date-time" },
        "collection_method": { "type": "string" },
        "license_id": { "type": "string", "enum": ["ODbL-1.0", "CC-BY-4.0", "CC0-1.0"] },
        "attribution": { "type": "string" },
        "redistribution_allowed_fields": { "type": "array", "items": { "type": "string" } }
      }
    }
  }
}
```

### 5.2 Deduplication & Entity Resolution Hierarchy
1. **Tier 1 (Source Exact)**: Match on composite key `(provenance.source_dataset, identity.source_id)`. Immediate deduplication.
2. **Tier 2 (Barcode Exact)**: Match on normalized GTIN-13 (converting UPC-A via prepended `0` and verifying Luhn check digit). Cross-source duplicates are preserved as separate records linked by `same_as: [id1, id2]` to maintain license purity.
3. **Tier 3 (Model Match)**: Match on `(observed.brand, identity.mpn)` where MPN $\ge 4$ alphanumeric characters.
4. **Tier 4 (Fuzzy String Similarity)**: Flag candidate pairs with token Jaccard similarity $> 0.85$ for human/algorithmic audit. **Never merge automatically**, adhering to the rule against merging products based solely on title similarity.

### 5.3 Benchmark Taxonomy & Relevance Protocol (`benchmark-queries.jsonl`)
The benchmark implements the 16 query failure modes across four script categories: Bengali (`bn`), English (`en`), Banglish (`bl`), and Mixed (`mx`).

```
                              ┌────────────────────────────────────────┐
                              │     Candidate Pooling Engine           │
                              │  (BM25 Lexical + Dense Bi-Encoder +    │
                              │         Hard Distractor Filter)        │
                              └──────────────────┬─────────────────────┘
                                                 │
                                                 ▼
                              ┌────────────────────────────────────────┐
                              │       Consensus Annotation Layer       │
                              │   (Model A: Claude 3.5 Sonnet          │
                              │    Model B: GPT-4o / Antigravity)      │
                              └────────┬───────────────────────┬───────┘
                                       │ Agree                 │ Disagree
                                       ▼                       ▼
                        ┌─────────────────────────┐  ┌─────────────────┐
                        │   Annotated Consensus   │  │ Human Adjudicator│
                        │    (ai_consensus)       │  │ (Native Speaker)│
                        └──────────────┬──────────┘  └─────────┬───────┘
                                       │                       │
                                       ▼                       ▼
                        ┌──────────────────────────────────────────────┐
                        │    Stratified Human Verification Sample      │
                        │    - 100% of bn, bl, mx Script Queries       │
                        │    - 100% of Grade-3 Exact Matches           │
                        │    - Compute Cohen's Kappa Inter-Rater Score │
                        └──────────────────────────────────────────────┘
```

#### The 16 Evaluated Query Types:
1. **Exact Product Name**: Verbatim product title lookup (Script: `bn`, `en`).
2. **Partial Product Title**: Truncated title prefix/infix (e.g., "Pran mango juice 250ml").
3. **Brand Searches**: Broad navigational intent (e.g., "Aarong", "AmazonBasics").
4. **Category Searches**: Broad taxonomy navigation (e.g., "সরিষার তেল", "wireless mouse").
5. **Attribute-Constrained**: Specs filter (e.g., "red cotton t-shirt size XL").
6. **Bengali Query vs. Bengali Record**: Native script query against native record.
7. **English Query vs. English Record**: Baseline English catalog retrieval.
8. **Bengali Query vs. English Record**: Cross-lingual retrieval (e.g., "তারবিহীন মাউস" $\rightarrow$ "Wireless Mouse").
9. **English Query vs. Bengali Record**: Cross-lingual retrieval (e.g., "mustard oil" $\rightarrow$ "খাঁটি সরিষার তেল").
10. **Mixed Script (Code-Mixed)**: Intra-query mixing (e.g., "Samsung মোবাইল ফোন cover").
11. **Transliteration (Banglish)**: Romanized Bengali queries (e.g., "shorishar tel", "chaler bag").
12. **Typographical & Phonetic Errors**: Keyboard layout slips and phonetic misspellings (e.g., "samusng moblie", "প্রান জুস").
13. **Synonyms & Semantic Search**: Functional equivalents (e.g., "mosquito repellent" $\rightarrow$ "মশার কয়েল").
14. **Multi-Constraint Combinations**: Complex predicates (Brand + Color + Size + Price boundary).
15. **Hard Negatives / Distractors**: Near-miss products differing by a single incompatible attribute (e.g., iPhone 15 case vs. iPhone 15 Pro case).
16. **Unanswerable / Out-of-Catalog**: Valid e-commerce queries with zero catalog fulfillment; evaluates precision at k and false-positive suppression.

#### Graded Relevance Mapping (ESCI Alignment):
- **Grade 3 (Exact)**: Matches all explicit and implicit query constraints.
- **Grade 2 (Substitute)**: Meets the core user intent; functional equivalent differing in minor secondary attribute.
- **Grade 1 (Complement/Partial)**: Does not satisfy the query standalone, but serves as a related accessory or partial match.
- **Grade 0 (Irrelevant)**: Fails core intent or violates negative constraints.

---

## 6. Recommendations

### 6.1 Data Acquisition & Production Strategy
1. **Curate a 10,000-Record Core Composite Dataset**:
   - Extract all ~1,336 Bangladesh-tagged items from Open Food Facts plus ~3,664 international FMCG items with verified GTIN barcodes under ODbL 1.0.
   - Extract 5,000 diverse non-food records (electronics, apparel, kitchen) from Amazon Berkeley Objects under CC BY 4.0.
   - Maintain distinct partition folders (`data/off_odbl/` and `data/abo_ccby/`) to prevent license cross-contamination, linking them via a unified metadata index.
2. **Deterministic Bengali Enrichment Pipeline**:
   - Filter Wikidata CC0 entity labels to map canonical English-Bengali brand and category dictionaries.
   - Employ Aksharantar phonetic models to generate top-3 Banglish phonetic spelling variants for all Bengali titles, tagging them explicitly as synthetic derived forms.
3. **Benchmark Corpus Size**:
   - Construct 500 validated queries (minimum 25 queries per type across the 16 taxonomy categories).
   - Require dual AI consensus for initial grading, backed by mandatory native Bengali speaker verification for 100% of `bn`, `bl`, and `mx` query judgments.

### 6.2 Foreman Architectural Improvements
1. **Implement Development Mode Handoff**:
   Author a follow-up Foreman task under `mode: development` to execute data extraction scripts, run GTIN/Unicode validators, and compile final artifact bundles into `deliverables/`.
2. **Persist Execution Telemetry in Event Logs**:
   Patch [`src/orchestrator/engine.py:760`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L760) to record `res.duration`, `res.cost_usd`, and session tokens in `step_done` and `finished` events, surfacing them via `foreman show <id> --metrics`.
3. **Introduce Adversarial Peer Review to R&D**:
   Update [`modes/research.md`](file:///var/www/projects/orchestrator/modes/research.md) to support a `challenge` investigate step executed by a competing provider family, verifying citations and empirical claims before document finalization.
4. **Expand Truncated Context Buffers**:
   Modify [`engine.py:833-836`](file:///var/www/projects/orchestrator/src/orchestrator/engine.py#L833-L836) to retain up to 1,000 characters for structured findings when inter-step dependencies are explicitly declared.

---

## 7. Open Questions for the User
1. **Transition Authorization**: Do you authorize initializing a follow-up Foreman task in `development` mode to allow file generation and Python execution for downloading, normalizing, and packaging the dataset?
2. **License Distribution Policy**: Is an ODbL 1.0 Share-Alike copyleft requirement on the FMCG partition acceptable for your downstream search engine testing, or must all data be strictly under permissive licenses (CC0 / CC-BY / Apache-2.0)?
3. **Daraz Mendeley Dataset Utility**: Should the Daraz BD Mendeley dataset (CC BY-NC 4.0 with TDM reservation) be evaluated strictly for private offline test baselines, with an explicit prohibition against public distribution?
4. **Human Verification Bandwidth**: Can native Bengali speakers be allocated to review and adjudicate the ~250 Bengali, transliterated, and code-mixed benchmark query judgments?
