# BeaconMFG MCP —— 容器化运行镜像（stdio 传输）
#
# 用途：让 Glama 之类的目录在容器里把服务拉起来并做工具内省/评分。
# 本服务是**纯 Python 标准库实现，零第三方依赖**，所以没有 pip install 步骤，
# 构建只需拷贝 mcp/ 目录，秒级完成。
#
# 协议：JSON-RPC 2.0 over stdio（newline-delimited）。
# 直接以 server.py 作为容器入口，stdout 只走协议，不加任何日志包装。
#
# 运行示例（本地验证，构建上下文是仓库根）：
#   docker build -t beacon-mfg-mcp .
#   printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
#     | docker run -i --rm beacon-mfg-mcp

FROM python:3.12-slim

# PYTHONUTF8：中文数据按 UTF-8 读写，不依赖宿主 locale
# BEACON_MASK_PHONE：对外回执默认脱敏手机号（与公开分发口径一致）
ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BEACON_MASK_PHONE=1

WORKDIR /app

# server.py 的路径相对自身文件解析，与 cwd 无关；这里整目录拷贝即可
COPY mcp/ /app/mcp/

# 寻源内核：npm 包靠 prepack 拷进 mcp/rfq-kernel，git 里没有这份副本
# （mcp/rfq-kernel/ 在 .gitignore 内）。server.py 的查找顺序是
# ../skills/rfq-kernel/src → rfq-kernel/src，所以按平级布局放进 /app/skills 即可，
# 不必在镜像里制造第二份副本。缺了它 start/answer/refine_sourcing 三个工具会降级。
COPY skills/rfq-kernel/ /app/skills/rfq-kernel/

# stdio 协议直接挂在容器主进程上
CMD ["python", "/app/mcp/server.py"]
