/* Generated from frontend/ui-protocol.py. Do not edit. */

export type UrnLianghuaUiV1 = UiAction | UiRequest | UiState | UiMessage | UiProblem | UiOperation | UiFrame;
export type ActionId = string;
export type Kind = "edit" | "select" | "query" | "invoke" | "reset" | "dismiss";
export type Target = string;
/**
 * This interface was referenced by `Extensions`'s JSON-Schema definition
 * via the `patternProperty` "^(.*:.*|x-.*)$".
 *
 * This interface was referenced by `Extensions1`'s JSON-Schema definition
 * via the `patternProperty` "^(.*:.*|x-.*)$".
 */
export type JsonValue =
  | boolean
  | number
  | string
  | JsonValue[]
  | {
      [k: string]: JsonValue;
    }
  | null;
export type Operation = string | null;
export type RequestId = string;
export type Operation1 = string;
export type Schema = string;
export type Revision = number;
export type Revision1 = number;
export type MessageId = string;
export type Kind1 = "resource" | "collection" | "series" | "document" | "operation" | "event" | "problem" | "custom";
export type Type = string;
export type Schema1 = string;
export type CorrelationId = string | null;
export type Title = string | null;
export type Role = string | null;
export type Path = string;
export type Label = string | null;
export type Role1 = string | null;
export type Format = string | null;
export type Unit = string | null;
export type Precision = number | null;
export type Order = number | null;
export type Visible = boolean | null;
export type Fields = FieldHint[];
export type Rel = string;
export type Href = string;
export type Type1 = string | null;
export type Links = UiLink[];
export type Protocol = "lianghua.ui";
export type Version = 1;
export type Messages = UiMessage[];
export type Type2 = string;
export type Code = string;
export type Title1 = string;
export type Detail = string | null;
export type Field = string | null;
export type Retryable = boolean;
export type Problems = UiProblem[];
export type Available = boolean;
export type Status = "idle" | "pending" | "succeeded" | "failed";
export type RequestId1 = string | null;
export type Revision2 = number;
export type Operation2 = string;
export type InputSchema = string;
export type ResponseSchemas = string[];
export type Mode = "read" | "write";
export type Kind2 = "action" | "request" | "response" | "state" | "close";
export type Messages1 = UiMessage[];

export interface UiAction {
  action_id: ActionId;
  kind: Kind;
  target: Target;
  data?:
    | boolean
    | number
    | string
    | JsonValue[]
    | {
        [k: string]: JsonValue;
      }
    | null;
  operation?: Operation;
}
export interface UiRequest {
  request_id: RequestId;
  operation: Operation1;
  schema: Schema;
  data: JsonValue;
}
export interface UiState {
  revision?: Revision;
  targets?: Targets;
}
export interface Targets {
  [k: string]: UiSlot;
}
export interface UiSlot {
  revision?: Revision1;
  values?: Values;
  selection?:
    | boolean
    | number
    | string
    | JsonValue[]
    | {
        [k: string]: JsonValue;
      }
    | null;
  query?: Query;
  messages?: Messages;
  problems?: Problems;
  operations?: Operations;
}
export interface Values {
  [k: string]: JsonValue;
}
export interface Query {
  [k: string]: JsonValue;
}
/**
 * 后端和前端之间唯一的通用语义消息。
 */
export interface UiMessage {
  message_id: MessageId;
  kind: Kind1;
  type: Type;
  schema: Schema1;
  data: JsonValue;
  correlation_id?: CorrelationId;
  presentation?: Presentation | null;
  links?: Links;
  extensions?: Extensions;
  protocol?: Protocol;
  version?: Version;
}
/**
 * 可选的语义呈现提示。
 */
export interface Presentation {
  title?: Title;
  role?: Role;
  fields?: Fields;
}
/**
 * 给显示层的字段语义提示。
 *
 * 这里描述数据的含义，不描述 React 组件、HTML、CSS 或布局。
 */
export interface FieldHint {
  path: Path;
  label?: Label;
  role?: Role1;
  format?: Format;
  unit?: Unit;
  precision?: Precision;
  order?: Order;
  visible?: Visible;
}
/**
 * 消息中的关系链接，不规定具体传输方式。
 */
export interface UiLink {
  rel: Rel;
  href: Href;
  type?: Type1;
}
export interface Extensions {
  [k: string]: JsonValue;
}
/**
 * 可放入 ``problem`` 消息正文的通用问题。
 */
export interface UiProblem {
  type: Type2;
  code: Code;
  title: Title1;
  detail?: Detail;
  field?: Field;
  retryable?: Retryable;
  extensions?: Extensions1;
}
export interface Extensions1 {
  [k: string]: JsonValue;
}
export interface Operations {
  [k: string]: UiOperationState;
}
export interface UiOperationState {
  available?: Available;
  status?: Status;
  request_id?: RequestId1;
  revision?: Revision2;
  input?:
    | boolean
    | number
    | string
    | JsonValue[]
    | {
        [k: string]: JsonValue;
      }
    | null;
  result_input?:
    | boolean
    | number
    | string
    | JsonValue[]
    | {
        [k: string]: JsonValue;
      }
    | null;
}
export interface UiOperation {
  operation: Operation2;
  input_schema: InputSchema;
  response_schemas: ResponseSchemas;
  mode?: Mode;
}
export interface UiFrame {
  kind: Kind2;
  action?: UiAction | null;
  request?: UiRequest | null;
  messages?: Messages1;
  state?: UiState | null;
}

export interface FrontendPort {
  dispatch(action: UiAction): Promise<UiState>;
  snapshot(): UiState;
  subscribe(listener: () => void): () => void;
}

export interface BackendPort {
  send(request: UiRequest): Promise<UiMessage[]>;
}

export interface UiProtocol {
  receive(message: UiMessage): void;
}
