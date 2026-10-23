/** 后端响应结构的前端镜像（只声明界面用到的字段）。 */

export type Space = {
  id: number;
  name: string;
  iri: string;
  domain: string;
  description: string;
  status: string;
};

export type Counts = {
  spaces: number;
  object_types: number;
  documents: number;
  chunks: number;
  proposals: number;
  pending_proposals: number;
  wiki_pages: number;
  agent_runs: number;
  memory_items: number;
};

export type StatusPayload = { counts: Counts; health: Record<string, Record<string, unknown>> };

export type ObjectTypeRow = {
  id: number;
  iri: string;
  label: string;
  label_en: string;
  definition: string;
  kind: string;
  status: string;
  abstract: boolean;
  confidence: number;
  origin: string;
  business_domain: string;
  parent_id: number | null;
  tags: string[];
  aliases: string[];
  version_no: number;
};

export type Evidence = {
  id: number;
  proposal_id: number | null;
  chunk_id: number | null;
  doc_id: number | null;
  doc_name: string;
  locator: string;
  quote: string;
  field_path: string;
  score: number;
};

export type TypeDetail = {
  id: number;
  space_id: number;
  iri: string;
  label: string;
  label_en: string;
  definition: string;
  kind: string;
  abstract: boolean;
  status: string;
  origin: string;
  confidence: number;
  business_domain: string;
  tags: string[];
  aliases: string[];
  annotations: Record<string, unknown>;
  version_no: number;
  parent: { id: number; label: string } | null;
  children: { id: number; label: string; status: string }[];
  properties: {
    id: number | null;
    label: string;
    datatype: string;
    unit: string;
    pattern: string;
    required: boolean;
    enum: string[];
    description: string;
  }[];
  links: {
    link_id: number;
    relation: string;
    category: string;
    direction: "in" | "out";
    target_id: number;
    target_label: string;
    cardinality: string;
    description: string;
  }[];
  constraints: {
    id: number;
    name: string;
    kind: string;
    expression: string;
    severity: string;
    reg_ref: string;
    description: string;
  }[];
  evidence: Evidence[];
};

export type Proposal = {
  id: number;
  space_id: number;
  run_id: number | null;
  kind: string;
  title: string;
  payload: Record<string, unknown>;
  rationale: string;
  status: string;
  confidence: number;
  review_note: string;
  applied_ref: string;
  created_at: string;
  evidence: Evidence[];
  duplicates: { id: number; label: string; score: number }[];
};

export type Candidate = {
  chunk_id: number;
  doc_id: number;
  doc_name: string;
  doc_type: string;
  locator: string;
  ref: string;
  lexical_rank: number | null;
  dense_rank: number | null;
  lexical_score: number;
  dense_score: number;
  fused_score: number;
  final_score: number;
  rank: number;
  reasons: string[];
  preview: string;
};

export type Citation = {
  ref: string;
  chunk_id: number;
  doc_id: number;
  doc_name: string;
  doc_type: string;
  locator: string;
  page: number | null;
  quote: string;
  statement: string;
  support: number;
  score: number;
  strength: string;
  rank: number;
};

export type Understood = {
  original: string;
  rewritten: string;
  intent: string;
  answer_shape: string;
  expansions: string[];
  sub_questions: string[];
  filters: Record<string, unknown>;
  matched_types: string[];
  notes: string[];
};

export type AskResponse = {
  answer_id: string;
  answer: string;
  citations: Citation[];
  candidates: Candidate[];
  understood: Understood;
  coverage: { coverage: number; [key: string]: number };
  citation_count: number;
  provider: string;
};

export type DocumentRow = {
  id: number;
  name: string;
  doc_type: string;
  doc_format: string;
  issuer: string;
  jurisdiction: string;
  effective_date: string;
  authority: number;
  status: string;
  error: string;
  char_count: number;
  chunks: number;
  created_at: string;
};

export type WikiPageRow = {
  id: number;
  slug: string;
  title: string;
  page_type: string;
  object_type_id: number | null;
  current_version: number;
  locked_by_human: boolean;
  dirty: boolean;
  revision_count: number;
  updated_at: string;
};

export type WikiPageDetail = WikiPageRow & {
  body_md: string;
  author: string;
  change_note: string;
  revisions: RevisionRow[];
};

export type RevisionRow = {
  id: number;
  version_no: number;
  author: string;
  change_note: string;
  digest: string;
  chars: number;
  created_at: string;
};

export type CompileReport = {
  created: number;
  updated: number;
  unchanged: number;
  skipped_locked: number;
  pages: string[];
};

export type DiffPayload = {
  page_slug: string;
  left: RevisionRow;
  right: RevisionRow;
  unified_diff: string;
  added_lines: number;
  removed_lines: number;
  side_by_side: { left: string; right: string; kind: string }[];
};

export type GraphNodeType = "object_type" | "data_property" | "document" | "relation_type";

export type GraphNode = {
  id: string;
  type: GraphNodeType;
  ref_id: number;
  label: string;
  sublabel: string;
  kind: string;
  status: string;
  domain: string;
  degree: number;
  weight: number;
  confidence?: number;
  definition?: string;
  required?: boolean;
  authority?: number;
};

export type GraphEdge = {
  id?: string;
  source: string;
  target: string;
  relation: string;
  category: string;
  weight: number;
  cardinality?: string;
  origin?: string;
  description?: string;
  locator?: string;
  quote?: string;
  hop?: number;
};

export type GraphStats = {
  node_count: number;
  edge_count: number;
  type_count: number;
  nodes_by_type: Record<string, number>;
  edges_by_category: Record<string, number>;
  density: number;
  avg_degree: number;
  orphans: string[];
  orphan_count: number;
  components: number;
};

export type GraphPayload = {
  space_id?: number;
  center?: string;
  depth?: number;
  nodes: GraphNode[];
  edges: GraphEdge[];
  stats: GraphStats;
};

export type GraphPath = {
  found: boolean;
  hops: number;
  path: { from: string; to: string; relation: string; direction: string }[];
  labels: string[];
};

export type GraphInsights = {
  stats: GraphStats;
  hubs: { label: string; degree: number; id: number }[];
  isolated: string[];
  coverage: { types_without_evidence: string[]; count: number };
  recent_proposals: { id: number; title: string; status: string; confidence: number }[];
  domain_distribution: Record<string, number>;
};

export type SkillRow = {
  name: string;
  display: string;
  description: string;
  max_steps: number;
  example: string;
  allowed_tools: string[];
  tools: string[];
};

export type ToolRow = {
  name: string;
  description: string;
  risk: string;
  parameters: Record<string, { type: string; required: boolean; description: string }>;
};

export type AgentRunRow = {
  id: number;
  goal: string;
  status: string;
  steps_used: number;
  created_at: string;
  summary: string;
};

export type AgentStepRow = {
  ordinal: number;
  thought: string;
  action: string;
  action_input: Record<string, unknown>;
  observation: string;
  status: string;
  latency_ms: number;
};

export type MemoryRow = {
  id: number;
  kind: string;
  scope: string;
  content: string;
  keywords: string[];
  salience: number;
  use_count: number;
  source_run_id: number | null;
  created_at: string;
};

export type ComponentItem = {
  kind: string;
  name: string;
  display: string;
  description: string;
  configured: boolean;
  local_fallback: boolean;
  config_hint: Record<string, string>;
  capabilities: string[];
};

export type ComponentGroup = {
  kind: string;
  label: string;
  active: string;
  count: number;
  items: ComponentItem[];
};

export type ComponentCatalog = {
  groups: ComponentGroup[];
  total: number;
  settings: Record<string, string | number | boolean | null>;
};

export type ProbeResult = Record<string, string | number | boolean | null | undefined> & {
  kind: string;
  elapsed_ms: number;
  ok?: boolean;
  error?: string;
};

export type DeployPayload = {
  targets: ComponentItem[];
  current: { app_name: string; data_dir: string; database_url: string; cors_origins: string[] };
};

export type ComponentHealth = Record<string, Record<string, string | number | boolean>>;

export type WebSearchResult = { title: string; url: string; snippet: string; source: string };

export type SourceTable = { name: string; columns: string[]; rows: unknown[][]; meta: Record<string, unknown> };

export type DatasourceDescribe = {
  uri: string;
  adapter: string;
  error?: string;
  tables: SourceTable[];
};

export type ChunkRow = {
  id: number;
  ordinal: number;
  locator: string;
  token_estimate: number;
  text: string;
  meta: Record<string, unknown>;
};

export type DocumentsPayload = {
  documents: DocumentRow[];
  formats: string[];
  total_chunks: number;
};

export type IngestReport = {
  doc_id: number;
  name: string;
  chunks: number;
  chars: number;
  elapsed_ms: number;
  doc_format: string;
  warnings: string[];
  [key: string]: unknown;
};

/** Agent SSE 事件的宽松类型：字段随事件类型变化，界面按 type 分支取用。 */
export type StreamEventPayload = Record<string, unknown> & {
  type: string;
  run_id?: number | null;
  ordinal?: number | string;
  thought?: string;
  action?: string;
  observation?: string;
  ok?: boolean;
  repaired?: string;
  remaining?: number | string;
  message?: string;
  answer?: string;
  status?: string;
  steps?: number | string;
  tokens?: number | string;
  elapsed_ms?: number | string;
  proposals?: { id: number; title: string }[];
  memory_item_id?: number | null;
  tools?: string[];
  memory_hits?: string[];
  proposal_id?: number | string;
  summary?: string;
};

export type SandboxConfig = {
  enabled: boolean;
  timeout_s: number;
  max_output: number;
  allowed_imports: string[];
};

export type SandboxRun = {
  ok: boolean;
  stdout: string;
  stderr: string;
  exit_code: number;
  elapsed_ms: number;
  violations: string[];
};

export type McpServer = {
  name: string;
  display: string;
  transport: string;
  enabled: boolean;
  description: string;
  tools: { name: string; qualified: string; description: string; parameters: Record<string, unknown> }[];
};

/** 单个本体空间的规模统计。 */
export type SpaceStats = {
  object_types: number;
  active_types: number;
  draft_types: number;
  deprecated_types: number;
  data_properties: number;
  relation_types: number;
  links: number;
  constraints: number;
  enumerations: number;
  pending_proposals: number;
  documents: number;
  by_status: Record<string, number>;
  by_domain: Record<string, number>;
  by_kind: Record<string, number>;
};

/** 随领域包发布的示例语料（/api/system/samples 与包目录共用同一行结构）。 */
export type SampleFileRow = {
  pack: string;
  pack_label: string;
  filename: string;
  doc_type: string;
  issuer: string;
  jurisdiction: string;
  authority: number;
  size_bytes: number;
};

export type PackSpaceRow = {
  id: number;
  name: string;
  domain: string;
  iri: string;
  stats: SpaceStats;
};

export type PackDescriptor = {
  key: string;
  label: string;
  summary: string;
  space_name: string;
  business_domains: string[];
  kinds: Record<string, string>;
  facets: Record<string, string>;
  standards: string[];
  classification: Record<string, string>;
  type_count: number;
  link_count: number;
  samples: string[];
  agent_hint: string;
  installed: boolean;
  space: PackSpaceRow | null;
  sample_files: SampleFileRow[];
  samples_available: number;
};

export type PackInstallReport = {
  pack: string;
  pack_label: string;
  space_id: number;
  space_name: string;
  seed: { created: number; types: number } | { skipped: string };
  samples?: { ingested: { filename: string; doc_id: number; chunks: number }[]; skipped: string[]; total_chunks: number };
  wiki?: { created: number; updated: number; unchanged: number; skipped_locked: number };
  stats: SpaceStats;
};

export type PackVocab = {
  key: string;
  label: string;
  summary: string;
  space_name: string;
  agent_hint: string;
};

/** 空间感知的受控词表：内核词汇 + 当前空间所属领域包的扩展维度。 */
export type Vocab = {
  object_kinds: Record<string, string>;
  datatypes: Record<string, string>;
  relation_categories: Record<string, string>;
  constraint_kinds: Record<string, string>;
  severities: Record<string, string>;
  classification: Record<string, string>;
  standards: string[];
  facets: Record<string, string>;
  business_domains: string[];
  sample_questions: string[];
  pack: PackVocab;
};
