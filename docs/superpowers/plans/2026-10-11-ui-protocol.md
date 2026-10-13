# 通用前端交互协议实现计划

## Goal

把 `frontend/ui-protocol.py` 变成前端内核唯一的、与后端业务无关的消息协议。
后端只需要产生协议消息，`frontend/display` 以后只需要消费协议消息并决定如何呈现。
CLI、Web、React 和 HTTP/WebSocket/SSE 都不进入本协议核心。

## Architecture

```text
后端数据
  -> 传输适配器
  -> UiMessage（唯一语义协议）
  -> frontend/display
  -> 具体显示技术
```

消息使用一个通用信封，正文由 `kind`、`type`、`schema` 和 `data` 描述。
消息类别包括 resource、collection、series、document、operation、event、problem、custom。
呈现信息只保存语义提示，不保存 React、HTML、CSS 或布局树。

## Tech Stack

- Python 3.12+
- dataclasses
- JSON-compatible values
- unittest
- JSON Schema 作为跨语言线格式的后续事实来源

## Spec

### UiMessage

- `protocol`: 协议名称，默认 `lianghua.ui`
- `version`: 协议版本
- `message_id`: 消息唯一标识
- `correlation_id`: 可选的请求/事件关联标识
- `kind`: 通用消息类别
- `type`: 语义类型标识
- `schema`: 正文的 JSON Schema 标识
- `data`: 只允许 JSON 值
- `presentation`: 可选的语义呈现提示
- `links`: 可选的关系链接
- `extensions`: 可选的命名空间扩展

### Presentation

只允许标题、语义角色和字段提示。字段提示可以说明标签、格式、单位、精度、顺序和可见性。
不允许保存组件名、CSS、HTML、JS、JSX 或布局结构。

### Custom

自定义消息必须使用自定义 `type` 和 `schema`。扩展键必须带命名空间。
未知类型必须能被前端明确报告为不支持；不能静默猜测，也不能执行动态代码。

## Global Constraints

- 不包含回测、策略、数据集、账户或其他后端领域名词。
- 不把传输协议误写进语义协议。
- 不增加兼容分支和业务特判。
- 不让 Python 类型成为跨语言的第二份线格式真相；对象必须能稳定序列化成 JSON。
- 保留用户已确定的文件名 `frontend/ui-protocol.py`。
- 本次不改后端、CLI、Web、React 或 `frontend/display`。

## Review Focus

- 同一消息能否被不同入口和不同后端使用。
- 消息类别是否足够通用，且没有把呈现技术写进协议。
- JSON 序列化是否稳定，非法值是否尽早失败。
- 自定义扩展是否有清晰边界。
- 测试是否覆盖普通消息、语义提示、事件关联、问题消息和非法输入。

## Tasks

- [x] 先写协议核心的失败测试：消息创建、JSON 映射、类别、呈现提示、扩展和非法输入。
- [x] 实现最小通用数据模型和序列化方法。
- [x] 更新协议说明，使文档和实现使用同一套字段与边界。
- [x] 运行针对性测试，再运行项目测试并记录与本次改动无关的失败。

## Validation

- 针对性测试：9 个测试通过。
- Python 编译检查：通过。
- `git diff --check`：通过。
- 完整 `unittest`：31 个测试中 1 个导入失败。失败来自已有的
  `frontend.api.test_fetch_manager` 使用顶层 `import fetch_manager`，与本次协议文件无关。
