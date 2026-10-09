"""会话交互（hook）：终端里 AI 提问/权限请求经 ntfy 到手机作答。

模块对应 a4phone 的 hook 链路：
  dispatch  分发器（事件路由、模式、宿主等待上限）
  handlers  提问作答 / 权限审批 / 任务完成通知
  transcript 会话记录抽取 AI 最后输出
  response  ntfy 响应话题订阅（手机作答回传）
  deskqueue 桌面弹窗队列（hook 写请求，a4agent 常驻进程代发）
"""
