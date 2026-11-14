# Pocklo 单首歌曲接入契约 v1

本地实现，部署须确认。后端使用 Python 3.11 或更高版本。生产 API 基址为 https://disk.6-79.cn ，规范分享页为 https://pocklo.jyonn.space/share/<token> 。本次未修改 Frienden 仓库。

## 双方边界

小兜保存原音频、独立歌曲元数据、封面、歌词和分享授权，签发短期私有 CDN 地址。编辑歌曲不改写原文件。普通音频和录音保持文件语义，用户确认“建立歌曲”后才具有歌曲语义，不使用特殊文件夹约定。

Frienden 需实现音乐卡片、播放器、歌词显示、元数据请求、签名刷新及失效状态。既有 OAuth JSON 请求仍由 Frienden 后端承载。音乐文件从 CDN 直接到浏览器，不经 Frienden 中转。不能假设 Frienden 已实现新增字段或播放器。

## 稳定身份与版本

song_id 是 UUID，version 为整数修订版，schema 固定为 pocklo.song.v1。audio_resource_id 关联音频，cover.id 为自选封面资源 ID 或派生封面 UUID，lyrics.id 为歌曲歌词的稳定 UUID。

改名和移动不改变关联。修改歌曲、重新关联音频、删除关联资源以及派生封面可用性变化增加 version。源音频删除后歌曲保留，available=false，播放请求返回 410。重新关联本人的音频即可恢复同一歌曲。删除歌曲删除其歌曲分享，保留原文件。

替换音频保留歌名、歌手、专辑和编辑后的歌词，时长建议与内嵌封面来源改为新音频。解析还未完成时 duration_ms 可以为 null，在编辑器确认时长。普通资源 API 不提供原地覆盖音频存储对象的能力。

## 公开歌曲信息

GET /api/song-shares/<token> 不需要登录或 OAuth。

HTTP 200，普通 API 使用既有 envelope。下面是本地样例的一次实际输出，重建样例时 UUID 以输出文件为准。

    {
      "code": 0,
      "body": {
        "schema": "pocklo.song.v1",
        "type": "song",
        "song_id": "f2fd25a9-8354-4e42-bd75-7fc9f58b46b8",
        "version": 1,
        "available": true,
        "audio_resource_id": "demomp",
        "title": "小兜联调静音曲",
        "artists": ["Pocklo Demo"],
        "album": "本地联调",
        "duration_ms": 20850,
        "cover": null,
        "cover_source": "embedded",
        "lyrics": {
          "id": "4d33b063-f3e0-496e-b7cb-7d8972ae833d",
          "language": "zho",
          "type": "lrc",
          "text": "[00:00.00]本地生成的静音样例\n[00:02.00]用于验证播放与拖动",
          "cues": [
            {"time_ms": 0, "text": "本地生成的静音样例"},
            {"time_ms": 2000, "text": "用于验证播放与拖动"}
          ]
        },
        "share_url": "http://localhost:8000/share/31f98de1-1e99-4182-8b4f-851357a85543"
      }
    }

duration_ms 允许 null，artists 允许空数组，album 允许空字符串。cover 为空表示缺封面，即使 cover_source 选了尚不存在的内嵌封面。有封面时为 {"id":"<稳定资源ID或派生UUID>","mime":"image/jpeg"}，没有长期图片地址。

cover_source 为 resource、embedded 或 null。lyrics.type 为 none、text、lrc 或 synchronized。同步数据统一以 cues 的毫秒时间为准，纯文本 cues 为空。language 是可选的 ID3 语言字段。lyrics.text 不得作为 HTML 渲染。

## 短期播放地址

GET /api/song-shares/<token>/playback 不需要登录。按需签发 300 秒的私有媒体地址。

    {
      "code": 0,
      "body": {
        "schema": "pocklo.song.v1",
        "song_id": "f2fd25a9-8354-4e42-bd75-7fc9f58b46b8",
        "version": 1,
        "expires_at": "2026-11-14T05:05:00+00:00",
        "expires_in": 300,
        "audio": {
          "resource_id": "demomp",
          "mime": "audio/mpeg",
          "url": "https://<private-cdn>/<encoded-path>?e=<epoch>&token=<signature>"
        },
        "cover": null
      }
    }

这里媒体 URL 是结构示意，没有真实生产签名。有封面时 cover 包含稳定 id、mime 和临时 url。签名 URL 必然含 CDN 对象路径，长期元数据不单独暴露原始存储 key、存储访问密钥、用户 Token 或 OAuth 口令。

Frienden 长期保存 share_url、song_id 和 schema。临时 URL 仅存内存，不能写入长期帖子或分享数据。

播放流程：

1. 展示时重新读取公开元数据，播放时按需调用 playback。
2. audio 元素使用 crossorigin="anonymous"、controls、preload="metadata"，src 直接为 CDN 地址。
3. 播放或跳转前剩余有效期不足 15 秒时重取。记录 currentTime、播放状态、音量，更换 src 后在 loadedmetadata 恢复。
4. CDN 403 或媒体错误时最多自动刷新一次，随后提供人工重试，避免循环。媒体错误码不能单独证明是过期。
5. playback 返回的 version 变化时重取元数据。404、410 停止刷新和播放。
6. 已缓冲内容可继续播放，新的 Range 请求仍需有效签名，不需要定时打断正常播放。

## 分享范围、撤销与缓存

一份歌曲分享只授权该歌曲、关联音频及其当前封面和歌词。歌词数据嵌入歌曲元数据，不开放原文件所在目录的列表、面包屑或其他私人资源。封面关联本人的普通资源也不会使其所在目录公开。

DELETE /api/shares/<token> 沿用既有撤销入口，仅所有者可调用。我的分享列表包含歌曲分享。

撤销后新的元数据和 playback 请求返回 404，不再签发 URL。已经签发的 CDN 地址最多继续有效 300 秒，已缓冲或已下载的字节不能收回。源文件删除也不等于 CDN 缓存即时清空。

所有歌曲接口返回 no-store/no-cache 响应头。可以按 (song_id,version) 做内存去重，但展示或播放前应重新确认分享有效性，不能靠长期缓存绕过撤销。普通 API 必须先判断 HTTP 状态，再读 body，不能仅判断 code=0。

错误响应示例，HTTP 409：

    {"code":0,"body":{"error":"version_conflict"}}

| HTTP | error | 处理 |
| --- | --- | --- |
| 400 | invalid_json / invalid_resource_id / unknown_field | 修正参数 |
| 400 | audio_required / image_required / invalid_lyrics / invalid_duration / invalid_artists / invalid_title | 校验资源类型或字段 |
| 401 | login_required | 仅管理接口，要求登录 |
| 404 | song_not_found / resource_not_found | 不存在或不属于当前用户，隐藏存在性 |
| 404 | share_not_found | 不存在、撤销或并非歌曲分享 |
| 409 | version_conflict | 重读后人工合并，不能盲目覆盖 |
| 409 | audio_already_has_song | 音频已关联另一首歌曲 |
| 410 | audio_unavailable | 源音频删除，显示不可播放 |
| 413 | metadata_too_large | JSON 超过 256 KB |

非法 UUID 由路由返回 404，可能不是 JSON。通用旧 API 与 OAuth 的原有错误 envelope 不变。

## OAuth 与 iframe 选择器

复用 files.read 和 shares.create，不引入 Frienden 专属权限。

GET /api/oauth/files/ 与 GET /api/oauth/files/<resource_id>/ 的歌曲条目在原字段上追加：

    {
      "id":"demomp",
      "name":"demo.mp3",
      "folder":false,
      "preview":"audio",
      "type":"song",
      "song":{
        "song_id":"f2fd25a9-8354-4e42-bd75-7fc9f58b46b8",
        "version":1,
        "title":"小兜联调静音曲"
      }
    }

GET /api/oauth/songs/<song_id>/ 要求 Bearer files.read，且是授权用户自己的歌曲。返回完整契约，未选择某个分享时 share_url=null。OAuth 成功响应是直接 JSON，没有普通 API 的 code/body 包装。

POST /api/oauth/shares/ 保持 {"resource_id":"demomp"} 与 Idempotency-Key。已确认歌曲新建歌曲分享，首次 HTTP 201，相同重试 200。返回原有 token、resource_id、name、folder、created_at、url，追加 type="song" 和完整 song 契约。url 格式仍为 /share/<token>。撤销后复用同一幂等键返回既有 share_revoked 410，不同资源复用同一键返回 idempotency_conflict 409。

iframe 的 pocklo.picker.selected 消息在 url 上追加 type 和 song 摘要。歌曲为 song，普通文件、文件夹分别为 file、folder。委托宿主仍只需返回 {url}，小兜根据当前条目附加类型。

Frienden 的改动边界为透传新字段、按 type 渲染音乐卡片、读取上述公开 API，并完成播放器与地址刷新。原文件列表可能没有 type/song 字段，消费者需兼容缺省。

旧音频分享不自动升级成歌曲分享，继续走旧分享页和 15 分钟的普通预览地址。旧 token 调新 song-shares 接口返回 404。旧目录和普通文件分享保持原语义，用户将音频建立为歌曲后，新建的分享才具有歌曲范围。

## 所有者接口与编辑

全部使用原有 Token 请求头，只允许本人管理，没有给第三方应用编辑特权。

| 方法与路径 | 输入或输出 |
| --- | --- |
| GET /api/audio/<resource_id>/metadata | status 为 pending/parsing/ready/failed，extracted、has_embedded_cover、song |
| POST /api/audio/<resource_id>/metadata | 重试失败解析 |
| POST /api/songs/ | 至少 audio_resource_id，默认带入解析结果，歌名兜底文件名 |
| GET /api/songs/<song_id> | 当前契约 |
| PUT /api/songs/<song_id> | 必带当前 version |
| POST /api/songs/<song_id>/shares | 创建歌曲范围的分享 |
| DELETE /api/songs/<song_id> | 删除歌曲和其分享，保留文件 |

更新示例：

    {
      "version":1,
      "title":"修改后的歌名",
      "artists":["歌手甲","歌手乙"],
      "album":"",
      "cover_resource_id":null,
      "use_embedded_cover":true,
      "lyrics":{"type":"lrc","text":"[00:01.20]第一句\n[00:03.00]第二句"}
    }

其他可更新字段有 duration_ms、audio_resource_id。cover_resource_id 为本人的 PNG/JPEG/WebP 普通资源，null 清除自选封面，再由 use_embedded_cover 决定是否使用内嵌封面。lyrics 编辑接受 none/text/lrc，保留内嵌 synchronized 时省略 lyrics。规范化 cues 由服务端生成。

编辑器可导入已有音频内嵌结果、上传补充封面、导入 LRC/纯文本、上传替换音频或选择已有音频资源 ID。解析失败或等待时可以手动填写，不要求重复上传内嵌内容。

## 解析与素材清理

Mutagen 解析 ID3 v1/v2.2/v2.3/v2.4，保留标签提供的歌手数组，不猜测斜杠是否表示多位歌手。时长从 MPEG 帧信息读取。APIC 正面封面优先，Pillow 校验并派生最长 1200px 的 JPEG。USLT 区分普通文本和 LRC。SYLT 支持毫秒以及可确定 MPEG 帧时长的格式，统一转换为毫秒 cues。

上传回调提交后安排最多两个后台解析线程，超额任务保存数据库。失败不影响文件上传和原音频播放。进程重启后的恢复依靠管理命令。解析限 64 MB/45 秒下载，封面输入限 8 MB/2000 万像素，歌词限 100000 字符，超限可手动编辑。

派生封面保存在既有私有桶的 disk/song-covers/<uuid>.jpg，单独 SongCover 记录，不出现在用户目录列表。只由合法歌曲播放请求签发访问地址。音频删除后派生记录脱离源对象，提交后尝试清理，失败保留 DB 引用供重试。已有自选封面保留普通资源身份，删除歌曲不会误删其原图。

部署批准后需要迁移和定时调度，本次未安装生产调度：

    python manage.py migrate
    python manage.py process_audio_metadata
    python manage.py clean_song_covers

后两条建议每分钟执行。低延迟线程不能代替持久任务恢复。

生产 CDN 只读验收命令：

    python manage.py verify_song_cdn --token <有效歌曲分享token> --origin https://frienden.jyonn.space

核对 audio MIME、Range 请求的 206/Content-Range、消费 origin 的 CORS。CDN 应暴露 Accept-Ranges、Content-Range、Content-Length，禁止破坏字节偏移的压缩。Frienden 的每个实际镜像 origin 都须验收。本次未向生产私有桶上传联调文件，也未更改生产 CDN 配置，本地模拟结果不代表生产配置已通过。

## 可运行联调样例

    cd disk-frontend
    npm ci
    npx ng build --configuration development
    cd ../disk-backend
    python -m pip install -r requirements.txt
    python scripts/song_demo.py --directory /tmp/pocklo-song-demo

打开 http://localhost:8000/__song_demo ，本地按钮登录隔离账号并进入歌曲编辑界面。匿名分享 token、真实 ID 和完整契约在启动日志及 /tmp/pocklo-song-demo/song.json。demo.mp3 是本地生成的约 20 秒静音 MPEG 文件，可验证完整播放，无外部歌曲版权依赖。

生产 CDN 配置、真实 MP3 和实际 Frienden 站点的联合验收仍是部署后的检查项。

联调 CDN 在 4 秒后过期，便于测试错误刷新；生产契约仍为 300 秒。该服务仅监听本机，不能当生产部署脚本使用。重新启动会复用歌曲并补建已撤销的样例分享。

测试命令：

    python manage.py test Resource.test_songs Resource.tests OAuth.tests

使用隔离测试数据库，覆盖解析、所有者权限、分享范围、撤销、版本、派生清理、OAuth 选择和旧分享兼容。


## 本地验证记录（2026-11-14）

28 个后端测试通过。Chrome 使用真实本地 Django 与隔离 SQLite，验证编辑器自动填入信息、保存后版本增加、匿名 MP3 播放到结尾、进度拖动、模拟 CDN 的跨域 206、过期 403 后重取，以及撤销后停止签发。另验证 390×844 手机视口的浅色/深色歌曲页和居中可滚动编辑器。

前端生产构建和开发构建通过，迁移状态检查无遗漏。生产 CDN、真实公开域名和 Frienden 音乐卡片尚未联合验收。此功能未部署，未改生产数据库、存储权限、CDN 配置或 Frienden 仓库。
