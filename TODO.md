- [x] 审查chat模块代码
- [x] 更新README
- [x] 更新提示词系统（模块化配置）
  把 #sym:BaseAgentConfig 中的 #sym:system_prompt_file 移除，改为每个子类单独设置。

  对于chat模块的聊天Agent，完全重构系统提示词，改为模块化：指定提示词目录、提示词文件顺序，并支持字符串模板，提供一些内置的注入变量。
- [ ] 定时任务（system_reminder实现）
- [ ] 记忆系统+会话压缩一起设计
- [ ] 移除`openai-proxy`
- [x] 内存占用测试
- [x] 考虑文件编辑工具、shell方案：使用mirage+sandlock
- [ ] 聊天、总结按Token计费
- [x] 卡池更新
- [ ] nonebot-plugin-githubcard
- [ ] env设置打印/不打印banner，包括Kanade和PydanticAI（在第一次运行agent时）
- [x] chat配置结构重构
- [ ] 检查gacha十连图片是否被缩放