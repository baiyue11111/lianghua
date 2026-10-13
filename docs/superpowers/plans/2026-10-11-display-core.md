# 统一交互核心实施记录

执行依据：用户在本会话批准的《交互层核心：统一动作、统一状态、两个协议面》。

目标：`frontend/display` 提供一份可在浏览器和 Node 中运行的 TypeScript 核心。
CLI 和 React Web 只适配前端动作及显示状态。`frontend/ui-protocol.py` 是协议的唯一手工来源。
后端适配保持独立，本次未装配真实后端时明确报错，测试替身不进入生产代码。

1. 协议：添加动作、状态、请求和端口；导出 JSON Schema、生成 TypeScript；跨语言样本验证。
2. 核心：实现动作更新、操作注册、schema 校验、请求执行、关联、过期响应、会话及资源清理。
3. Web：用外部状态订阅接入核心；清除组件直连 HTTP 与请求调度；保留 DOM 和图表行为。
4. CLI：参数转为动作，JSON Lines 启动同一 Node 核心；打包构建产物；明确错误、退出及进程回收。
5. 验收：核心与入口契约、原模型行为、编译、协议生成一致性、依赖边界与独立审查。

理论依据：[Presentation Model](https://martinfowler.com/eaaDev/PresentationModel.html)、
[Ports and Adapters](https://alistair.cockburn.us/hexagonal-architecture)、
[Clean Architecture chapter 22](https://www.informit.com/articles/article.aspx?p=2832399&seqNum=2)、
[DDD Reference](https://www.domainlanguage.com/wp-content/uploads/2016/05/DDD_Reference_2015-03.pdf#page=17)、
[Elm Architecture](https://guide.elm-lang.org/architecture/)。

实施完成，改动留在当前工作区，未自动提交。

实施决策：
- 使用当前工作区并保留既有修改；后续提交需要区分本次和已有改动。
- 真实后端不在本次装配；生产操作明确返回“未连接”，接线前不能执行业务。
- 独立核心持有自己的 package.json 和锁文件；独立构建需要维护这份依赖清单。
- 删除旧 API 自动启动、重启及直接调用客户端；旧的自动后端启动方式退出。

独立审查发现的四项问题已修复，并有回归验证：检查绑定具体输入；恢复既有审核；来源切换更新状态；同输入刷新保留表单和焦点。

最终验证（2026-10-11）：
- 核心 39 项、Python 契约/入口/边界 31 项、原有模型 14 项，共 84 项定向测试通过。
- 策略、研究、审核三组挂载浏览器交互测试通过，使用测试后端，没有监听端口。
- 六个原计算模块的 SHA256 与迁移前相同；固定样本行为测试通过。
- Web 类型检查、构建、修改范围 ESLint、协议和运行产物一致性、git diff --check 通过。
- wheel 含最新核心和协议；从独立临时目录运行发布 CLI，无需 npm 或源代码树。
- 中文 HTML 框架图的三个视角、桌面/手机布局、源文件链接通过。

验收：前端核心及入口契约通过。真实后端接线待完成。

[中文框架图](../../interaction-core.html) · [入口及边界说明](../../interaction-entrypoints.md)
