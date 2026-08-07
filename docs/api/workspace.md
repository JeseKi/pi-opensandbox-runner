# Workspace API

Manager 提供三组不同权限边界的文件接口。

## 受限 Instance 工作区

固定在 `/root/workspace`，拒绝绝对路径、父目录跳转和符号链接逃逸：

```http
GET    /v1/instances/{subject_ref}/workspace-files
GET    /v1/instances/{subject_ref}/workspace-files/content
PUT    /v1/instances/{subject_ref}/workspace-files/content
DELETE /v1/instances/{subject_ref}/workspace-files/content
POST   /v1/instances/{subject_ref}/workspace-files/upload
```

读取需要 `workspace:read`，修改需要 `workspace:write`。更新和删除要求完整读取所得 `If-Match`；
新建上传要求 `If-None-Match: *`。

## Session workspace proxy

```http
GET|PUT|DELETE /v1/instances/{subject_ref}/sessions/{session_id}/workspace/{path}
```

路径相对 Session cwd。该接口是内部预览能力，路径前缀不是强安全边界。

## 完整容器文件系统

`/v1/instances/{subject_ref}/filesystem...` 接受绝对容器路径，需要高权限
`filesystem:access`，只应提供给可信后端。
