# macOS OrbStack 部署

这份文档只记录已经验证的 macOS 路径。Windows 暂不在本阶段范围内；Compose
文件本身使用标准 Docker 语法，但不宣称已在 Windows 验证。

## 1. 组件

```text
浏览器
  -> deviceops:8000
       -> firmware-rag:8011
       -> host.docker.internal:11434 (Ollama)
       -> XDP MCP HTTPS
            -> 授权测试设备
```

`firmware-rag` 使用两个 Docker named volume：

- `rag-vector-store`：Qdrant 本地索引。
- `rag-embedding-cache`：Embedding 缓存。

脱敏私有 Markdown 语料通过只在本机存在的目录挂载，不会打进镜像。

## 2. 前置条件

1. 安装并启动 OrbStack。
2. 安装 Ollama。
3. 下载两个本地模型：

```bash
ollama pull llama3.1:8b
ollama pull qwen3-embedding:0.6b
```

4. 确认 RAG 私有语料存在：

```text
../firmware-knowledge-agent/var/private-corpus/sources.json
```

5. 在 DeviceOps `.env` 中配置运行模式和私有 XDP URL。不要把完整 URL
   粘贴到命令行、文档或提交记录。

需要切换多台授权设备时，在 `device-agent-lab/var/` 放置私有
`device-profiles.json`，并在 `.env` 中增加：

```dotenv
DEVICE_PROFILES_FILE=/data/device-profiles.json
```

容器只持久化当前配置 ID，不会把 PSN、TOKEN 或完整 URL 写入选择
状态文件。

## 3. 构建和启动

在 `device-agent-lab` 目录执行：

```bash
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml ps
```

首次启动 RAG 会为 501 个 Chunk 建立本地向量索引，耗时取决于 Ollama 和机器
性能。健康检查在索引就绪后才通过，DeviceOps 随后启动。

## 4. 验证

```bash
curl http://127.0.0.1:8011/health
curl http://127.0.0.1:8000/health
```

浏览器入口：

- Firmware Knowledge Agent：`http://127.0.0.1:8011/`
- DeviceOps Agent：`http://127.0.0.1:8000/`

端到端只读验证：

```bash
curl -X POST http://127.0.0.1:8000/v1/requests \
  -H 'Content-Type: application/json' \
  -d '{"request":"诊断设备整体状态并结合文档给出建议","conversation_id":"compose-smoke"}'
```

验收条件：

- `status` 为 `completed`。
- Trace 同时出现设备证据采集和 `retrieve_knowledge`。
- 返回 `knowledge_sources` 且 `degraded=false`。
- DeviceOps `/health` 显示 `xdp`、`ollama`、`remote-rag`。

## 5. 端口冲突

不修改 Compose 文件，临时覆盖宿主端口：

```bash
DEVICEOPS_HOST_PORT=18000 \
FIRMWARE_RAG_HOST_PORT=18011 \
  docker compose -f deploy/compose.yaml up -d
```

容器内端口仍是 8000 和 8011。

## 6. 常见问题

### RAG 一直不健康

检查：

```bash
docker compose -f deploy/compose.yaml logs firmware-rag
ollama list
```

常见原因是宿主 Ollama 未启动、模型未下载、私有语料目录缺失，或首次建索引仍在
进行。

### DeviceOps 无法连接 RAG

容器内必须使用 `http://firmware-rag:8011`，不能使用
`http://127.0.0.1:8011`。Compose 已覆盖该变量。

### 容器无法连接 Ollama

Compose 使用 `http://host.docker.internal:11434`。先在宿主机确认：

```bash
curl http://127.0.0.1:11434/api/tags
```

### Qdrant 锁冲突

当前使用嵌入式 Qdrant，本地数据目录只允许一个进程持有锁。服务以单 worker
运行；若需要多副本，应改为独立 Qdrant Server，而不是共享同一本地目录。

## 7. 停止

```bash
docker compose -f deploy/compose.yaml down
```

该命令保留 named volumes。只有明确需要清空索引和缓存时才使用 `down -v`。
