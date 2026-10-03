# a4agent v0.5.0

## 本地模型（llama.cpp）

### 引擎升级至 llama.cpp b11370

- 引擎版本 b10919 → **b11370**（上游 2026-10-03 发布），带来 llama-server 原生 **System One 决策模型端点 `/v1/systemone`**
- 接入信息卡片与 `connect` 接口新增**决策模型端点展示**，局域网接入第三方客户端时一目了然
- CUDA 引擎包随上游 13.4 更名：`cuda133` → `cuda134`，旧配置标识自动映射，已配置用户无需任何调整
- 引擎包资产名与体积按上游 Release 页核对更新

## 其他

- 更新器 User-Agent 同步为 `a4agent-updater/1.0`
- 全新定稿品牌图标：前端页头/Favicon 与程序图标、安装器资源统一替换
- README：联系方式统一移至文末章节

## 校验

- 安装包：`a4agent-setup-0.5.0.exe`（21,078,040 字节）
- SHA256：`D2606DC766E5513FF55679E421824E1A56ED8F7ACAABAA45689796C6C2D87B8D`
