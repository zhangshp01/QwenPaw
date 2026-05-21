#!/bin/bash

# 启动 qwenpaw 应用（后台运行）
# 设置端口 8081，指定日志文件路径，启用重载模式

# 定义变量
LOG_FILE="./logs/log.log"
PID_FILE="./qwenpaw.pid"
# 设置工作区目录
QWENPAW_WORKING_DIR="/data/MADW/QwenPaw-zhgw/workspace"

# 检查是否已经在运行
if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
    echo "qwenpaw 已经在运行中 (PID: $(cat $PID_FILE))"
    exit 1
fi

# 创建日志目录（如果不存在）
mkdir -p "$(dirname "$LOG_FILE")"

# 设置环境变量并后台启动应用
echo "正在启动 qwenpaw..."
echo "工作区目录: $QWENPAW_WORKING_DIR"

# 导出环境变量并启动
export QWENPAW_WORKING_DIR="$QWENPAW_WORKING_DIR"
nohup qwenpaw app --port 8088 --log-file "$LOG_FILE" --reload >> "$LOG_FILE" 2>&1 &

# 记录进程 PID
echo $! > "$PID_FILE"

echo "qwenpaw 已启动"
echo "PID: $(cat $PID_FILE)"
echo "日志文件: $LOG_FILE"
echo "端口: 8088"
echo "工作区: $QWENPAW_WORKING_DIR"
