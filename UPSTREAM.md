# 来源与本版贡献

本项目基于 [didilili/shopkeeper-agent](https://github.com/didilili/shopkeeper-agent)，对应 [ai-agents-from-zero](https://github.com/didilili/ai-agents-from-zero) 的电商问数实战教程。

- 原始代码版本：`8045fa4`（原始本地 main 的起点）。
- 原作者：didilili。
- 原项目采用 MIT License；本仓库保留 [LICENSE](LICENSE) 中的完整版权和许可声明。
- 原教程说明存档于 [docs/upstream-readme.md](docs/upstream-readme.md)。当前配置和运行说明以本仓库根目录 README 为准。

原项目提供教学数仓、元数据建模、混合检索、LangGraph 问数流程、FastAPI 与 React 展示基础。

本版继续实现和整理了：受约束的 SQL 执行与有限修复、多轮会话和状态持久化、会话切换时的执行管理、证据驱动的结果分析与有限补查、可复现数据扩充、冻结测试集和评测工具、回归测试以及敏感配置隔离。

首次上传使用清理后的独立快照，没有把原始 Git 历史一起上传。这样可以避免历史中的明文配置进入新仓库；原始历史仍留在开发者本地，来源及许可证不受影响。

项目说明区分已实现能力、已验证结果和后续计划，不将教程提供的功能全部归为独立原创，不以参考 SQL 的正确率代替 Agent 的端到端成绩。
