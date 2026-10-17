# Web 界面

- 行情：搜索、K 线、指数；采集为轻量状态与动作。
- 策略研究：策略列表 → 配置尝试 → 单次结果。结果分为概览、配置和成交。
- 配置文件回测：从策略首页按需打开，使用既有服务器配置路径端口。没有网页参数编辑器。

所有请求经过现有交互核心。展示模型在 `src/research/model.ts`；格式工具在 `src/shared/format.ts`。适配层、协议和后端保持只读。

运行验证：

```sh
npm run test:dev
npm run build -- --outDir /tmp/readable-web-build
```

构建只做无缓存类型检查，不向依赖链接写入，也没有在 TypeScript 配置里写临时路径。

受控浏览器检查页：

```sh
node scripts/visual.mjs
```

命令输出临时 HTML 路径。此页使用真实组件与交互核心，后端是固定测试替身，不连接生产服务。可用页面片段：`#market`、`#research`、`#attempts`、`#result`、`#config`、`#trades`、`#batch`。其中批次结果仅调用测试替身。其他写操作被测试替身拒绝。

组件测试通过服务端渲染调用真实事件，未替换组件或 hooks；它不验证浏览器生命周期、实际图表绘制或手机尺寸。桌面截图仅检查了受控尝试列表。
