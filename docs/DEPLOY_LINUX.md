# QwenPaw-zhgw Linux 部署手册

本文档说明如何在**一台空的 Linux 服务器**上部署本仓库（**QwenPaw-zhgw**，含公文写作等定制能力）。

| 部署方式 | 适用场景 | 是否包含 zhgw 定制 |
| -------- | -------- | ------------------ |
| **Docker（源码构建）** | 生产、环境隔离 | ✅ 推荐 |
| **源码 + systemd** | 深度调试、无 Docker | ✅ |
| **官方镜像 `agentscope/qwenpaw`** | 快速试用官方版 | ❌ 不含本仓库改动 |

默认服务端口：**8088**

### 路径约定（避免占用系统盘）

本手册假定业务与数据均放在 **`/data/MADW`**（数据盘），系统盘仅保留 OS、Docker 引擎与 Nginx 等少量组件。可按实际挂载点调整，但请保持「代码 / 运行时数据 / Conda / Docker 数据」分离。

| 用途 | 路径 |
| ---- | ---- |
| 项目源码 | `/data/MADW/QwenPaw-zhgw` |
| 运行时数据（配置、记忆、Skills、备份） | `/data/MADW/qwenpaw-runtime/` |
| Conda 安装前缀 / 环境 | `/data/MADW/miniconda3` 或 `/data/MADW/conda-envs/QWENPAW-ZHGW`（见 §4.1，以 `echo $CONDA_PREFIX` 为准） |
| Docker 持久化目录（可选） | `/data/MADW/docker`（见 §3.4 说明） |

建议在 shell 中导出（后续命令均以此为准）：

```bash
export MADW_ROOT=/data/MADW
export PROJECT_ROOT=/data/MADW/QwenPaw-zhgw
export QWENPAW_DATA_ROOT=/data/MADW/qwenpaw-runtime
```

裸机默认工作目录为 **`$QWENPAW_DATA_ROOT/working`**（通过 `QWENPAW_WORKING_DIR` 设置），**不要**使用 `~/.qwenpaw`（位于用户家目录，通常在系统盘）。

---

## 1. 服务器要求

### 1.1 操作系统

- 推荐：**Ubuntu 22.04 / 24.04** 或 **Debian 12**
- 架构：x86_64 或 arm64

### 1.2 硬件（建议）

| 场景 | CPU | 内存 | 磁盘 |
| ---- | --- | ---- | ---- |
| 仅云端 API（通义等） | 2 核+ | **4 GB+** | 20 GB+ |
| 含浏览器/Playwright、公文生成 | 4 核+ | **8 GB+** | 40 GB+ |
| 本机跑大模型（Ollama 等） | 视模型而定 | 16 GB+ | 100 GB+ |

### 1.3 网络

- **出网**：访问大模型 API（如 DashScope）、可选 Tavily 等
- **入网**：按需开放 **8088**（或经 Nginx 反代 **443**）
- 若使用钉钉/飞书等频道，服务器需能访问对应开放平台

### 1.4 部署用户（可选）

```bash
sudo useradd -m -s /bin/bash deploy
sudo usermod -aG docker deploy   # 若使用 Docker
su - deploy
```

---

## 2. 系统初始化

在空机上执行：

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git ca-certificates gnupg lsb-release
```

### 2.1 安装 Docker（推荐）

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# 重新登录 SSH 或执行：
newgrp docker

docker --version
docker compose version
```

### 2.2 防火墙（对外提供 Web 时）

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
# 若直接暴露应用端口（不推荐生产裸奔）：
# sudo ufw allow 8088/tcp
sudo ufw enable
```

---

## 3. 方案 A：Docker 部署（生产推荐）

### 3.1 获取代码

```bash
export MADW_ROOT=/data/MADW
export PROJECT_ROOT=/data/MADW/QwenPaw-zhgw
export QWENPAW_DATA_ROOT=/data/MADW/qwenpaw-runtime

sudo mkdir -p /data/MADW
sudo chown $USER:$USER /data/MADW

# 代码已在 /data/MADW/QwenPaw-zhgw 时，直接：
cd "$PROJECT_ROOT"

# 首次部署时克隆或解压：
# git clone <你的-QwenPaw-zhgw-仓库-URL> "$PROJECT_ROOT"
# 或上传压缩包后解压到 /data/MADW/QwenPaw-zhgw
```

### 3.2 构建镜像

构建会编译前端控制台并打包 Python（耗时约 10–30 分钟，视网络与 CPU 而定）。镜像层默认写入 Docker 的 `data-root`；若系统盘空间有限，请先在 §3.4 将 Docker `data-root` 迁到 `/data/MADW/docker`。

构建前若脚本在 Windows 上传/编辑过，需先去掉 CRLF（否则会报 `$'\r': command not found` 且镜像未真正构建）：

```bash
cd "$PROJECT_ROOT"
find scripts deploy -name '*.sh' -print0 | xargs -0 sed -i 's/\r$//'
```

```bash
bash scripts/docker_build.sh qwenpaw-zhgw:v1.0.0
```

验证（应能看到完整 `docker build` 日志，且存在镜像）：

```bash
docker images | grep qwenpaw-zhgw
```

### 3.3 准备环境变量

```bash
mkdir -p "$PROJECT_ROOT/deploy-runtime"
cat > "$PROJECT_ROOT/deploy-runtime/.env" <<'EOF'
# 大模型 API（示例：通义）
DASHSCOPE_API_KEY=sk-xxxxxxxx

# 生产务必开启 Web 登录认证
QWENPAW_AUTH_ENABLED=true
QWENPAW_AUTH_USERNAME=admin
QWENPAW_AUTH_PASSWORD=请改为强密码

# 可选：其他工具密钥
# TAVILY_API_KEY=tvly-xxxxxxxx
EOF
chmod 600 "$PROJECT_ROOT/deploy-runtime/.env"

# 运行时数据目录（绑定挂载，落在数据盘）
mkdir -p "$QWENPAW_DATA_ROOT"/{working,secret,backups}
```

### 3.4 使用 Docker Compose 启动

在项目根目录创建 `docker-compose.prod.yml`。使用**宿主机目录绑定挂载**，避免 Docker 命名卷默认落在系统盘 `/var/lib/docker/volumes`：

```yaml
version: '3.8'

services:
  qwenpaw:
    image: qwenpaw-zhgw:latest
    container_name: qwenpaw
    restart: always
    env_file:
      - ./deploy-runtime/.env
    ports:
      # 生产建议只绑本机，由 Nginx 反代
      - "127.0.0.1:8088:8088"
    volumes:
      - /data/MADW/qwenpaw-runtime/working:/app/working
      - /data/MADW/qwenpaw-runtime/secret:/app/working.secret
      - /data/MADW/qwenpaw-runtime/backups:/app/working.backups
```

**（可选）将 Docker 引擎数据目录迁到数据盘**，减轻系统盘镜像/构建缓存压力：

```bash
sudo mkdir -p /data/MADW/docker
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "data-root": "/data/MADW/docker"
}
EOF
sudo systemctl restart docker
```

> 修改 `data-root` 后，原有镜像需重新 `docker pull` / `docker build`。仅应用数据已通过上述 bind mount 放在 `/data/MADW/qwenpaw-runtime`，与 `data-root` 无关。

启动：

```bash
cd "$PROJECT_ROOT"
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f
```

说明：

- 容器入口在**首次启动**且缺少 `config.json` 时会自动执行 `qwenpaw init --defaults`。
- 容器内应用监听 `0.0.0.0:8088`（见 `deploy/config/supervisord.conf.template`）。
- 配置、记忆、Skills 在 `$QWENPAW_DATA_ROOT/working`；密钥在 `secret/`；备份在 `backups/`。

### 3.5 宿主机上运行 Ollama（可选）

大模型在宿主机、应用在容器内时：

```bash
docker run -d --name qwenpaw ... \
  --add-host=host.docker.internal:host-gateway \
  ...
```

在控制台 **设置 → 模型** 中将 Base URL 设为 `http://host.docker.internal:11434`（Ollama）。

Linux 也可使用 `--network=host`（见 [README_zh.md](../README_zh.md) Docker 章节）。

---

## 4. 方案 B：裸机源码部署 + systemd

适用于无法使用 Docker、需要直接改代码调试的场景。**不需要 Conda 即可使用方案 A（Docker）。**

### 方案 B 进度（可按项勾选）

| 步骤 | 内容 | 章节 |
| ---- | ---- | ---- |
| ☐ | 验证 Conda 环境 `QWENPAW-ZHGW`（**已安装则只做本节**） | §4.1 |
| ☐ | 安装系统 apt 依赖 | §4.2 |
| ☐ | 构建前端 + `pip install` + Playwright | §4.3 |
| ☐ | `qwenpaw init` | §4.4 |
| ☐ | 复制 `gov-document-writer` Skill | §4.5 |
| ☐ | 手动试跑 `qwenpaw app` | §4.6 |
| ☐ | 配置 systemd 开机自启 | §4.7 |

Python 由 **Conda** 管理，版本需满足 **3.10 ≤ 版本 < 3.14**（见 `pyproject.toml`）。后续命令均在 **`conda activate QWENPAW-ZHGW`** 后执行。

### 4.1 验证 Conda 环境（已安装可从此节开始）

若你已完成 `conda create -n QWENPAW-ZHGW python=3.10`，**无需重复创建**，只需验证并记下路径：

```bash
export PROJECT_ROOT=/data/MADW/QwenPaw-zhgw
export QWENPAW_DATA_ROOT=/data/MADW/qwenpaw-runtime

conda activate QWENPAW-ZHGW
python --version                    # 应为 3.10.x
echo "CONDA_PREFIX=$CONDA_PREFIX"   # 配置 systemd 时用，不要手写猜路径
```

常见 `CONDA_PREFIX` 示例（以你机器上 `echo` 输出为准）：

- `/data/MADW/conda-envs/QWENPAW-ZHGW`（设置了 `CONDA_ENVS_PATH=/data/MADW/conda-envs`）
- `/data/MADW/miniconda3/envs/QWENPAW-ZHGW`（Miniconda 装在 `/data/MADW/miniconda3`）
- `/root/miniconda3/envs/QWENPAW-ZHGW`（装在系统盘时仍可运行，但不推荐）

验证失败时对照下表处理：

| 现象 | 处理 |
| ---- | ---- |
| `Could not find conda environment: QWENPAW-ZHGW` | 执行下方「§4.1 附录：首次创建环境」 |
| Python 不是 3.10.x | `conda install -n QWENPAW-ZHGW python=3.10` 或重建环境 |
| `conda: command not found` | `source /data/MADW/miniconda3/etc/profile.d/conda.sh`（路径按实际安装位置修改） |

<details>
<summary><b>§4.1 附录：首次创建 Conda 环境（未安装时展开）</b></summary>

```bash
# Miniconda 安装到数据盘（仅首次）
# bash Miniconda3-latest-Linux-x86_64.sh -b -p /data/MADW/miniconda3
# source /data/MADW/miniconda3/etc/profile.d/conda.sh

export CONDA_PKGS_DIRS=/data/MADW/conda-pkgs
export CONDA_ENVS_PATH=/data/MADW/conda-envs
mkdir -p "$CONDA_PKGS_DIRS" "$CONDA_ENVS_PATH"

conda create -n QWENPAW-ZHGW python=3.10 -y
conda activate QWENPAW-ZHGW
```

</details>

### 4.2 安装系统依赖（apt）

与 Conda 无关，仅需执行一次（不含 `python3` / `pip` / `venv`）：

```bash
sudo apt install -y \
  build-essential libssl-dev \
  nodejs npm \
  chromium-browser fonts-wqy-zenhei fonts-wqy-microhei \
  xvfb
```

### 4.3 构建并安装

```bash
export PROJECT_ROOT=/data/MADW/QwenPaw-zhgw
export QWENPAW_DATA_ROOT=/data/MADW/qwenpaw-runtime
conda activate QWENPAW-ZHGW
cd "$PROJECT_ROOT"

# 前端控制台（可将 npm 缓存指向数据盘）
export npm_config_cache=/data/MADW/npm-cache
cd console && npm ci && npm run build && cd ..
mkdir -p src/qwenpaw/console
cp -R console/dist/. src/qwenpaw/console/

# 在 Conda 环境中安装本项目
export PIP_CACHE_DIR=/data/MADW/pip-cache
pip install -U pip wheel
pip install -e ".[full]"

# Playwright 浏览器（Docker 镜像已内置，裸机需安装）
export PLAYWRIGHT_BROWSERS_PATH=/data/MADW/playwright-browsers
python -m playwright install chromium
```

安装完成后确认 CLI 可用：

```bash
which qwenpaw
qwenpaw --version
```

### 4.4 初始化

```bash
conda activate QWENPAW-ZHGW
export PROJECT_ROOT=/data/MADW/QwenPaw-zhgw
export QWENPAW_DATA_ROOT=/data/MADW/qwenpaw-runtime
export QWENPAW_WORKING_DIR=/data/MADW/qwenpaw-runtime/working
mkdir -p "$QWENPAW_WORKING_DIR"
qwenpaw init --defaults --accept-security
```

### 4.5 安装公文写作 Skill（zhgw 定制）

```bash
SKILL_DST="$QWENPAW_WORKING_DIR/skills/gov-document-writer"
mkdir -p "$SKILL_DST"
cp -r "$PROJECT_ROOT/gov-document-writer/"* "$SKILL_DST/"
```

随后在控制台 **设置 → Skills** 中启用 `gov-document-writer`。

### 4.6 手动启动验证（建议先于 systemd）

```bash
conda activate QWENPAW-ZHGW
export QWENPAW_WORKING_DIR=/data/MADW/qwenpaw-runtime/working
export PLAYWRIGHT_BROWSERS_PATH=/data/MADW/playwright-browsers
cd /data/MADW/QwenPaw-zhgw
qwenpaw app --host 0.0.0.0 --port 8088
```

浏览器访问 `http://<服务器IP>:8088/`，完成 **设置 → 模型**（API Key）后再配置 systemd。

### 4.7 配置 systemd（开机自启）

先将 Conda 前缀写入变量（**必须使用 §4.1 中 `echo $CONDA_PREFIX` 的实际值**）：

```bash
conda activate QWENPAW-ZHGW
CONDA_PREFIX="$(echo "$CONDA_PREFIX")"
echo "$CONDA_PREFIX"   # 确认非空
```

再创建服务（把下面 `CONDA_PREFIX` 替换成上一步输出，或使用 `envsubst`）：

```bash
conda activate QWENPAW-ZHGW
CONDA_PREFIX="$CONDA_PREFIX"

sudo tee /etc/systemd/system/qwenpaw.service <<EOF
[Unit]
Description=QwenPaw zhgw
After=network.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/data/MADW/QwenPaw-zhgw
Environment=QWENPAW_WORKING_DIR=/data/MADW/qwenpaw-runtime/working
Environment=PLAYWRIGHT_BROWSERS_PATH=/data/MADW/playwright-browsers
Environment=QWENPAW_AUTH_ENABLED=true
Environment=QWENPAW_AUTH_USERNAME=admin
Environment=QWENPAW_AUTH_PASSWORD=请改为强密码
Environment=DASHSCOPE_API_KEY=sk-xxxxxxxx
Environment=PATH=${CONDA_PREFIX}/bin:/usr/bin:/bin
ExecStart=${CONDA_PREFIX}/bin/qwenpaw app --host 0.0.0.0 --port 8088
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now qwenpaw
sudo systemctl status qwenpaw
journalctl -u qwenpaw -f
```

> `User`/`Group` 若使用非 root 部署账号（如 `deploy`），请改为对应用户，并保证该用户对 `/data/MADW` 有读写权限。

---

## 5. 反向代理与 HTTPS

对外访问时，建议用 Nginx 将流量转发到本机 `127.0.0.1:8088`：

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

`/etc/nginx/sites-available/qwenpaw`：

```nginx
server {
    listen 80;
    server_name your.domain.com;

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/qwenpaw /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d your.domain.com
```

---

## 6. 部署后配置清单

| 步骤 | 操作 | 说明 |
| ---- | ---- | ---- |
| 1 | 访问 `http://<IP或域名>:8088/` | 经 Nginx 则用 `https://your.domain.com` |
| 2 | 登录 | 已设置 `QWENPAW_AUTH_*` 则用预设账号；否则首次在页面注册 |
| 3 | **设置 → 模型** | 配置 API Key 或本地模型；未配置则无法正常对话 |
| 4 | **设置 → Skills** | 启用 `gov-document-writer`（zhgw） |
| 5 | （可选）**设置 → 环境变量** | 如 `TAVILY_API_KEY` |
| 6 | （可选）频道 | 见 [频道配置](https://qwenpaw.agentscope.io/docs/channels) |

健康检查：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8088/
```

诊断（需已安装 CLI）：

```bash
qwenpaw doctor
```

---

## 7. 运维命令速查

```bash
# Docker
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml restart
docker compose -f docker-compose.prod.yml logs -f --tail=200

# 升级：拉代码后重新构建并重建容器
cd /data/MADW/QwenPaw-zhgw && git pull
bash scripts/docker_build.sh qwenpaw-zhgw:latest
docker compose -f docker-compose.prod.yml up -d --force-recreate

# systemd
sudo systemctl restart qwenpaw
sudo journalctl -u qwenpaw -n 100 --no-pager

# 查看运行时数据目录占用
du -sh /data/MADW/qwenpaw-runtime/*
df -h /data/MADW
```

---

## 8. 常见问题

| 现象 | 处理 |
| ---- | ---- |
| 8088 无法访问 | `ss -tlnp \| grep 8088`；检查防火墙、是否只绑定了 `127.0.0.1` |
| 页面能开但无法对话 | 在控制台配置模型与 API Key |
| 浏览器/网页工具失败 | Docker 镜像已含 Chromium；裸机需 `playwright install` 与系统 `chromium` |
| 容器连不上宿主机 Ollama | 使用 `host.docker.internal` 或 Linux `--network=host` |
| 忘记管理员密码 | 参考 [安全文档](https://qwenpaw.agentscope.io/docs/security) 处理认证配置 |
| Docker 构建很慢 | 配置镜像加速；确保 Node/Python 依赖源可访问 |
| 系统盘空间不足 | 确认数据在 `/data/MADW`；Docker 使用 `data-root`；Compose 使用 bind mount 而非默认 volume |
| `docker_build.sh` 报 `$'\r': command not found` | 见 §3.2：`sed -i 's/\r$//' scripts/*.sh deploy/*.sh` 后重试 |
| `conda activate` 后无 `qwenpaw` 命令 | 先完成 §4.3 `pip install -e ".[full]"`，且当前 shell 已 activate |

---

## 9. 附录：官方 QwenPaw 镜像（无 zhgw 定制）

若不需要本仓库的公文写作等改动，可直接使用官方镜像：

```bash
docker pull agentscope/qwenpaw:latest
# 国内可选：
# docker pull agentscope-registry.ap-southeast-1.cr.aliyuncs.com/agentscope/qwenpaw:latest

docker run -d --name qwenpaw --restart always \
  -p 127.0.0.1:8088:8088 \
  -v /data/MADW/qwenpaw-runtime/working:/app/working \
  -v /data/MADW/qwenpaw-runtime/secret:/app/working.secret \
  -v /data/MADW/qwenpaw-runtime/backups:/app/working.backups \
  --env-file /data/MADW/QwenPaw-zhgw/deploy-runtime/.env \
  agentscope/qwenpaw:latest
```

或使用仓库根目录的 `docker-compose.yml`。

---

## 10. 最小验收脚本

**Docker 部署：**

```bash
set -e
docker ps --filter name=qwenpaw --format '{{.Names}} {{.Status}}'
curl -sf -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8088/ || echo "FAIL"
df -h /data/MADW
```

**裸机（Conda 已安装后）：**

```bash
set -e
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate QWENPAW-ZHGW
python --version
which qwenpaw
qwenpaw --version
curl -sf -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8088/ || echo "（若未启动 app 可忽略）"
df -h /data/MADW
```

完成 HTTP 200/302 后，在浏览器配置 **设置 → 模型** 与 Skills。

---

## 相关文档

- [README_zh.md](../README_zh.md) — 项目概览与快速开始
- [scripts/README.md](../scripts/README.md) — Docker 构建脚本说明
- [官方快速开始](https://qwenpaw.agentscope.io/docs/quickstart)
- [官方安全与 Web 认证](https://qwenpaw.agentscope.io/docs/security)
