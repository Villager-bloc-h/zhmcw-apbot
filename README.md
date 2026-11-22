# mcw-apbot

中文 Minecraft Wiki **巡查豁免者时间线**的自动维护脚本（pywikibot）。

数据页：<https://zh.minecraft.wiki/w/User:Villager-bloc-h/ap.json>
（即 `{{timeline|page=User:Villager-bloc-h/ap.json}}` 读取的 JSON）

脚本每次运行只做两件**增量**工作，不重建 2017 年以来的历史：

1. **在任核查（Special:ListUsers）**：用 `list=allusers&augroup=autopatrol`
   （即 [Special:ListUsers?group=autopatrol](https://zh.minecraft.wiki/w/Special:ListUsers?group=autopatrol&limit=200)）
   取现任名单，与 JSON 中所有 `"end": "now"` **或结束日期晚于今天**的条目比对。
   - 仍在名单 → 结束日期必须是 `"now"`，被改成过去/未来日期时自动恢复；
   - 不在名单 → 把 `"now"` 或未来日期改成离任日期（优先取权限日志的显式除权；
     临时授权静默到期则取授权的到期日）；
   - 账号查不到 → 跟随 `renameuser` 日志改名；若注册日期晚于首次获权日期（用户名
     被他人重新注册），只告警、不改数据；
   - 在名单里但页面没有 → 取其最近一次获权日期，补一条以 `"now"` 结尾的记录。
2. **新授权补录**：从 `--since`（默认 `2026-10-01`）起扫描权限日志，把新增授权、
   窗口内除权与续期合并进 `bars`；仍在任的结束日期写 `"now"`。按开始日期去重，
   重复运行无副作用。
3. **列表页同步**：把同一份数据渲染到
   [User:Villager-bloc-h/Autopatrolled user](https://zh.minecraft.wiki/w/User:Villager-bloc-h/Autopatrolled_user)：
   - 按年份分组，组内按首次获权日期、再按用户名排序；
   - 保留页首、年份以外的章节（如“导航”），以及现有条目的链接写法与注记；
   - 离任且累计任职不超过 10 天的用户加斜体。

日期统一取 **UTC 日历日**；`legend` / `style` / `margins` 与历史条目原样保留；
序列化后与页面相同则直接退出，不产生编辑。写入前会校验页面结构，残缺条目会报错
退出而不是覆盖页面。

> 核对范围：`"now"` 与晚于今天的结束日期。已写成过去日期的离任记录不会逐条重算
> （除非该用户仍在名单中，会被恢复为 `"now"`）。

## 权限要求

`ap.json` 是 **JSON 内容模型**的用户页，机器人密码必须包含
`editmyuserjson`（编辑自己的用户 JSON），否则保存会被拒绝：

```
mycustomjsonprotected: You do not have permission to edit this JSON page.
```

在 <https://zh.minecraft.wiki/wiki/Special:BotPasswords> 创建/编辑机器人密码时勾选
“编辑自己的用户 JSON”（需要 HTTP 编辑权限时同时勾选“编辑现有页面”）。可选：
“高压编辑（bot）”让编辑带机器人标记。

## 快速开始

```bash
git clone https://github.com/<you>/mcw-apbot.git
cd mcw-apbot
python -m pip install -r requirements.txt

cp password.py.example password.py    # Windows: copy password.py.example password.py
# 编辑 password.py，填入 Special:BotPasswords 生成的密码

python ap_bot.py --dry-run            # 只看会改什么
python ap_bot.py                      # 有变化才保存
```

首次运行会自动登录，cookie 缓存在 `pywikibot-<用户名>.lwp`（已 git 忽略），
后续可无人值守运行。

Windows 也可以直接双击/计划任务调用 `run_bot.bat`；Linux/macOS 用 `./run_bot.sh`。

## 配置

- `user-config.py`
  - `family_files['minecraft'] = 'https://zh.minecraft.wiki/api.php'`：本 wiki 不在
    pywikibot 内置 family 中，由 AutoFamily 自动解析；
  - `usernames['minecraft']['minecraft']`：机器人**账号名**（不含 `@后缀`），可用
    环境变量 `MCW_BOT_USER` 覆盖。
- `password.py`（从 `password.py.example` 复制，**不要提交**）
  - 每行一个元组：`('账号', BotPassword('后缀', '密码'))`；
  - 字面量 `BotPassword` 是 pywikibot 的格式要求，不能删。
- `ap_bot.py` 顶部的 `PAGE_TITLE` 是默认页面，也可用 `--page` 临时指定。

## GitHub Actions 定时运行

仓库自带 `.github/workflows/update-timeline.yml`，每天 19:17 UTC（北京 03:17）
运行，也支持手动触发。

1. 把本目录推送到 GitHub；
2. 仓库 **Settings → Secrets and variables → Actions → New repository secret**：
   - `MCW_BOT_PASSWORD`：机器人密码（必填）；
3. 如需换账号，在 **Variables** 里加 `MCW_BOT_USER`（账号名）和
   `MCW_BOT_SUFFIX`（密码后缀，默认 `ap`）；
4. 在 **Actions → Update autopatrol timeline → Run workflow** 手动跑一次验证。

工作流只读取仓库、不提交任何文件；它通过 Python 在运行期生成临时的
`password.py`（密码来自 Secrets），运行结束后随 runner 销毁。

> 注意：GitHub 托管 runner 的出口 IP 偶尔可能被 wiki 的 WAF 拦截；若 Actions 中
> 登录/请求异常，改用自建 runner 或本地 cron。另外，仓库连续 60 天无活动时，
> GitHub 会自动暂停 schedule，需要到 Actions 页面重新启用。

## 本地定时

Linux（cron，每天 03:17）：

```cron
17 3 * * * cd /srv/mcw-apbot && ./run_bot.sh >> ap_bot.log 2>&1
```

Windows（任务计划程序）：操作选择 `run_bot.bat`，起始于填仓库目录；需要时把
`APBOT_PYTHON` 设为虚拟环境解释器的绝对路径。

## 参数与退出码

| 参数 | 说明 |
| --- | --- |
| `--dry-run` | 不保存，打印修改点与 unified diff |
| `--since YYYY-MM-DD` | 新授权扫描起点（含当天），默认 `2026-10-01` |
| `--list-members` | 只打印 Special:ListUsers 中的现任巡查豁免者名单后退出 |
| `--list-page TITLE` | 列表页标题（默认 `User:Villager-bloc-h/Autopatrolled user`） |
| `--skip-list` | 不同步列表页，只维护数据页 |
| `--skip-current` / `--skip-new` | 跳过对应步骤，便于排查 |
| `--page TITLE` | 指定其他数据页 |
| `--summary TEXT` | 自定义编辑摘要 |
| `--no-bot` | 不给编辑加 bot 标记 |

退出码：`0` 正常（含无变化）；`1` 配置/保存失败；`2` 编辑冲突。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `mycustomjsonprotected` | 机器人密码缺 `editmyuserjson`，见“权限要求” |
| 登录失败 / 一直要求密码 | 检查 `password.py` 的元组格式；删除 `pywikibot-*.lwp` 后重试 |
| `第 N 个条目缺少 text 标签` | 数据页被误编辑，先修复页面结构 |
| `保存冲突` | 页面刚被他人修改，稍后重跑即可 |

## 目录结构

```
mcw-apbot/
├── .github/workflows/update-timeline.yml
├── LICENSE
├── ap_bot.py
├── user-config.py
├── password.py.example
├── requirements.txt
├── run_bot.bat
├── run_bot.sh
└── README.md
```

## 说明

- 仅维护 `User:Villager-bloc-h/ap.json` 数据页与
  `User:Villager-bloc-h/Autopatrolled user` 列表页，不会改动条目正文或其他页面。

## 致谢与许可

- 本项目在 **DeepSeek-V4.1-Flash** 辅助下编写完成。
- 本项目内容（代码、文档与数据整理）采用 **CC BY-NC-SA 3.0**
  （署名—非商业性使用—相同方式共享 3.0 未本地化版本）授权：
  转载、修改或再分发时请保留署名与来源链接，以相同许可共享，且不得用于商业用途。
  - 许可全文：<https://creativecommons.org/licenses/by-nc-sa/3.0/legalcode>
  - 中文摘要：<https://creativecommons.org/licenses/by-nc-sa/3.0/deed.zh-hans>
