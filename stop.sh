#!/bin/bash

# 停止 qwenpaw 应用

PID_FILE="./qwenpaw.pid"
APP_NAME="qwenpaw"

# 检查 PID 文件是否存在
if [ ! -f "$PID_FILE" ]; then
    echo "未找到 PID 文件，$APP_NAME 可能未运行"
    # 尝试通过进程名查找并停止
    PIDS=$(pgrep -f "qwenpaw app.*--port 8081")
    if [ -n "$PIDS" ]; then
        echo "发现运行中的进程: $PIDS"
        read -p "是否停止这些进程? (y/n): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            kill $PIDS
            echo "已发送停止信号"
        fi
    fi
    exit 1
fi

# 读取 PID
PID=$(cat "$PID_FILE")

# 检查进程是否存在
if ! kill -0 "$PID" 2>/dev/null; then
    echo "进程 $PID 不存在，清理 PID 文件"
    rm -f "$PID_FILE"
    exit 0
fi

# 停止进程
echo "正在停止 $APP_NAME (PID: $PID)..."

# 先尝试优雅停止
kill "$PID"

# 等待进程结束（最多等待 10 秒）
for i in {1..10}; do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "$APP_NAME 已停止"
        rm -f "$PID_FILE"
        exit 0
    fi
    echo -n "."
    sleep 1
done

echo ""

# 如果进程仍在运行，强制终止
if kill -0 "$PID" 2>/dev/null; then
    echo "优雅停止失败，强制终止进程..."
    kill -9 "$PID" 2>/dev/null
    sleep 1
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "$APP_NAME 已强制停止"
        rm -f "$PID_FILE"
    else
        echo "错误：无法停止进程 $PID"
        exit 1
    fi
fi
