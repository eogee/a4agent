"""会话交互（hook）：终端里 AI 提问/权限请求经 ntfy 到手机作答。

模块对应 a4phone 的 hook 链路：
  dispatch  分发器（事件路由、模式、宿主等待上限）
  handlers  提问作答 / 权限审批 / 任务完成通知
  transcript 会话记录抽取 AI 最后输出
  response  ntfy 响应话题订阅（手机作答回传）
  deskqueue 桌面弹窗队列（hook 写请求，a4agent 常驻进程代发）

例外：WorkBuddy 的提问面板在用户作答前不触发 hook（实测全程没有 hook 子进程，
详见 app.workbuddy_listen 模块头）。该端的提问提醒改由常驻进程读会话 jsonl 发出，
本包只负责其余事件与其余六端。
"""
