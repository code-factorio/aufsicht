# Tool clusters

Every cluster below is reached with `search_tools` → `call_readonly_tool` / `call_tool`. Names given here are entry points, not a full inventory - `search_tools` is the authoritative list, so query it rather than assuming a tool is missing.

## Authorization matrix

Most clusters work the moment the user is signed in to Telecontext. These do not, and the user must connect them once in [Teleconsole → Authorization Services](https://teleconsole.telecontext.telekom.net/authorization-services):

| Cluster | Connection |
| --- | --- |
| `gitlab` | OAuth - click Connect, grant permissions |
| `chatbot-factory` | OAuth |
| `smnow` | OAuth |
| `jira` | Personal Access Token, generated in [Jira profile settings](https://jira.telekom.de/secure/ViewProfile.jspa?selectedTab=com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens) and pasted into Teleconsole |
| `confluence` | API token, pasted into Teleconsole |

`knowledge`, `terminus-*`, `tardis`, `psa`, `psa-verification`, `raccoon`, `leanix`, `feedback` and `time` need no extra connection.

## knowledge

`query_company_knowledge` returns ranked raw chunks, each carrying `filename`, `fullname`, `chunk` and `relevance`. It hands back retrieval, not an answer - the synthesis is yours.

## terminus-devtools, terminus-onearch

Each exposes the same three tools:

- `*_retrieve_context` - semantic search with reranking, `number_of_results` up to 20.
- `*_list_documents` - browse the corpus, optional case-insensitive `filter` on path, returns up to 50.
- `*_read_document` - full document text by path. Paths must start with `shared/`. Images come back as image content, everything else as extracted text.

Use `list_documents` → `read_document` when you need a whole document rather than the chunks retrieval hands you.

## tardis

Discover internal APIs exposed through the TARDIS platform.

1. `tardis_api_search` - search by name, abbreviation, or partial match. Returns up to 100 APIs with titles, descriptions, API IDs and available environments.
2. `get_tardis_api_details` - one API in one environment: team, hub, base path, visibility, spec links, health checks, monitoring, rate limiting. Environment-specific, so use production unless the user asks otherwise.
3. `get_tardis_openapi_spec` - the full OpenAPI document, fetched via the `yamlLink` or `jsonLink` from step 2.

Some teams expose their own MCP servers through TARDIS. Their tools carry a path-derived prefix (`bp_magentaqa_mcp_v1__confluence_search`, `eni_mockcp_v1__trigger_resource_list_change`) and are found the same way, with `search_tools`.

## psa, psa-verification

PSAHelper, reached through Telecontext. It analyzes and returns structured results; the judgement call stays with you and the user.

Requirement lookup:

- `psa_list_subject_areas_and_req_docs` - the requirement hierarchy.
- `psa_list_requirements` - requirements within a document.
- `psa_get_requirement` - one requirement in full.

Repository verification, a server-side agent:

1. `psa_verification_select_documents` - identify the documents applicable to a repository.
2. Have the user review that list and add or remove documents before proceeding.
3. `psa_verification_run` - verify the repository against each selected document. **Up to 15 minutes per document**; warn the user first.
4. `psa_verification_get_reasons` - detailed reasoning. Request it for non-compliant requirements only, or the response becomes unmanageable.

Verification reads the repository from GitLab, so local uncommitted work is invisible to it.

## gitlab

Read: `gitlab_search` across issues, MRs, code, wikis and users; project, group and subgroup listings; branches, commits, `gitlab_commit_compare`; `gitlab_repository_file_get` and its base64 variant; tags; `gitlab_wiki_list`.

Issues and epics: list, get, create, update, plus comment listing and creation on both.

Merge requests: `gitlab_merge_request_list`, and a full discussion API - `gitlab_merge_request_discussion_list`, `_create`, `_note_add`, `_resolve`. This is the cluster for reviewing an MR conversation without leaving the editor.

Pipelines: list, `gitlab_pipeline_start`, `gitlab_pipeline_retry`, bridge and job listings, `gitlab_pipeline_job_retry`, `gitlab_pipeline_job_erase`.

`gitlab_pipeline_start`, `_retry` and `_erase` consume runners and mutate CI history - confirm before running them.

## jira

`jira_search` takes JQL, which is the fastest route to anything non-trivial. Beyond it: `jira_get_issue`, `jira_create_issue`, `jira_batch_create_issues`, `jira_update_issue`, `jira_delete_issue`.

Transitions need two calls - `jira_get_transitions` for the ids valid on that issue right now, then `jira_transition_issue`. Guessing a transition id fails.

Also available: comments (`jira_get_comments`, `jira_add_comment`, `jira_edit_comment`), worklogs, issue links and epic links, boards and sprints (`jira_get_agile_boards`, `jira_get_board_issues`, `jira_get_sprints_from_board`, `jira_get_sprint_issues`, `jira_create_sprint`), projects and versions, `jira_search_fields` for custom field ids, `jira_get_issue_sla`, `jira_get_user_profile`.

## confluence

`confluence_search` for pages, blog posts and comments; `confluence_search_user`; `confluence_get_page` and `confluence_get_page_children`; `confluence_create_page`, `confluence_update_page`, `confluence_delete_page`; comments and labels.

`confluence_update_page` and `confluence_delete_page` overwrite and remove shared content - confirm the target page with the user first.

## leanix

Factsheet lookup by factsheet ID, name, or an associated email. Supported types: Application, Governance Object, Organization. This is the route to an ICTO number, an application's lifecycle status, or the person responsible for a system.

## smnow

ServiceNow at [smnow.telekom.de](https://smnow.telekom.de).

- Incidents: `smnow_incident_search`, `_create`, `_update`.
- Changes: `smnow_change_search`, `_get`, `_create`, `_update`, `smnow_change_template_search`, plus change tasks (`smnow_change_task_get`, `_create`, `_update`).
- CMDB: `smnow_ci_search`, `smnow_ci_relationships`.
- Generic: `smnow_query_table`, `smnow_get_record`, `smnow_get_stats` for aggregates and grouping.
- Users and knowledge articles: `smnow_user_search` and the knowledge tools.

Changes and incidents are production process records. Every create or update here is visible to operations teams - confirm before writing.

## raccoon

PromQL against TARDIS monitoring data in VictoriaMetrics, scoped to the public metric tenant `hub-eni-tardis-public`. Covers StarGate (API gateway), Horizon (event bus) and SkyGate (SOAP gateway).

`raccoon_list_metrics`, `raccoon_list_labels` and `raccoon_list_label_values` first, to ground the query in metrics that actually exist; then `raccoon_instant_query` for a single point or `raccoon_range_query` for a series.

Defaults to the PROD environment. Name any other environment explicitly in the query.

## chatbot-factory

- `chatbot_factory_list_chatbots` - accessible chatbots with IDs (format `cf-` plus 12 alphanumeric characters), names, descriptions and types.
- `chatbot_factory_query_chatbot` - ask a chatbot; runs LLM inference and analyses the query for you.
- `chatbot_factory_retrieve_document` - raw ranked chunks from the chatbot's knowledge base, no inference. No query analysis happens here, so supply keyword-rich terms yourself. Personal documents sit under `personal/`, shared under `shared/`.

## feedback

`submitFeedback` records feedback about agents with metadata. `askFeedbackQuestion`, `searchFeedback`, `getAllFeedbacks` and `getFeedbacksByRating` (1-5 stars) read it back for statistics such as top-rated agents over a period or recent bug reports for one agent.

## time

`get_time` returns the current time. Reach for it before any relative date computation - "last sprint", "the past 30 days", a PromQL range - rather than assuming today's date.
