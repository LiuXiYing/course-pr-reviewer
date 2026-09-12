# Course PR Reviewer

面向课程作业仓库的通用 GitHub PR 审核器。每个课程只维护作业配置和学生名单，身份校验、确定性规则、AI、OCR 和视觉审核由一个版本化 Action 复用。

> 当前状态：`0.10.9`。确定性规则、Gemini/GLM 双模型共识审核、可选本地 PaddleOCR、AI 错误说明、PR 评论、自动合并和超期关闭均已实现。

## 设计原则

- 学生身份来自经过教师确认的 GitHub 账号映射，不能由 PR 标题冒充。
- PR 标题仍然严格校验，便于教师直接查看。
- 只有 `PASS` 可以自动合并；不确定结果进入 `MANUAL_REVIEW`。
- 双模型均不可用、GitHub、OCR 或配置异常必须失败关闭；单模型临时不可用时显式降级并记录。
- 审核哪个 head SHA，就只能合并哪个 SHA。
- 每次审核都会发布一条新评论，使学生能在 Conversation 底部直接看到最新结果；评论末尾标明对应的 head SHA。
- 截止时间使用当前 head 提交被推送到 PR 的服务器时间（由调用方工作流写入 `event_at`），而不是 PR 首次创建时间，避免截止前开占位 PR、截止后补交；达到关闭阈值后自动评论并关闭。
- 共享审核器不保存课程密钥、学生名单或课程题目。
- 高权限工作流永远不检出或执行学生 PR 中的代码。

本版本不会创建教师兜底标签或自动分派人工任务。`MANUAL_REVIEW` 仍作为安全决策保留，它只会阻止自动通过。

评论、合并和关闭都有独立开关：

```yaml
features:
  auto_merge: true
  comment_review: true
  close_late_pr: true
```

只有 `PASS` 能触发合并。合并和关闭前会重新读取 PR，当前 head SHA 与已审核 SHA 不一致时立即跳过；旧工作流运行不会修改新提交。

## 课程仓库需要维护的文件

```text
.github/
├── course-review.yml
└── students.yml
```

`course-review.yml` 集中保存整个学期的作业配置，每次发布新作业时向 `assignments` 追加一项。`students.yml` 在开学时根据教师确认的 Excel 名单生成，学期中仅在账号变更时更新。

完整示例见：

- [`examples/course-review.yml`](examples/course-review.yml)
- [`examples/students.yml`](examples/students.yml)

## 从 Excel 生成学生名单

Excel 仅使用三列，第一行表头必须精确为：

| 学号 | 姓名 | github账号名 |
|---|---|---|
| 2023010102 | 刘西莹 | example-user |

安装 Excel 导入依赖并转换：

```bash
python -m pip install '.[excel]'
course-pr-reviewer import-students \
  --excel students.xlsx \
  --output .github/students.yml
```

默认不覆盖已有的 `students.yml`。确认覆盖时显式加 `--force`。导入器会拒绝空值、非 10 位学号、重复学号、重复 GitHub 账号、非法 GitHub 账号和多余列。

## 本地验证配置

要求 Python 3.11 或更高版本：

```bash
python -m pip install .
course-pr-reviewer validate \
  --config examples/course-review.yml \
  --students examples/students.yml
```

成功时输出：

```json
{"valid": true, "course": "数据结构", "assignments": ["Lab1", "Lab2"], "students": 2}
```

## 审核决策

| 决策 | 含义 | 自动合并 |
|---|---|:---:|
| `PASS` | 全部检查通过，含达到复核上限后的 AI 分歧放行 | 是 |
| `FAIL` | 存在有证据的明确违规 | 否 |
| `MANUAL_REVIEW` | 身份未登记、内容有歧义或模型不确定 | 否 |
| `ERROR` | GitHub、配置或外部服务异常 | 否 |

稳定的原因代码定义在 [`models.py`](src/course_pr_reviewer/models.py)，所有运行结果遵守 `review-result.schema.json`。

## Gemini 与 GLM 双模型审核

文本和图片阶段都可以配置 GLM 与 Gemini 两个提供商。两个模型每轮独立审核；结论一致时立即按该结论处理（均通过则通过，均拒绝则合并可复核的问题，均不确定则转人工）。结论不一致时，将上一轮的意见和证据作为线索重新核对原始材料，默认最多审核三轮；第三轮仍有分歧时，该阶段按规则视为通过。每轮的双方结论、理由和证据保存在阶段元数据的 `consensus.history[].findings` 中。一个模型临时不可用时采用另一个模型的结果并在 PR 评论中标记降级状态，两个模型均不可用时暂停合并。

`consensus_rounds` 控制最多审核几轮，双模型默认 `3`，可配置 `1` 至 `3`；单模型必须为 `1`。PR 评论分别显示实际次数与上限，例如“已审核 1 轮，最多 3 轮”；达到上限仍有分歧时，明确标注“按规则通过”。分歧放行仅作用于该 AI 阶段，账号、文件、截止时间、其他启用的审核阶段及合并前 head SHA 校验仍须通过。

缺少密钥、鉴权失败或模型名配置错误属于配置故障，不允许降级为单模型。API 超时、429 或服务端临时错误在内部重试耗尽后才属于可降级故障。

文本和图片审核遇到无效 JSON、缺少字段、空证据等输出格式错误时，会将具体字段和 Schema 约束反馈给原模型，最多自动纠正两次（加上首次生成，共三次）。每次仍使用相同的原始提交、审核规则和时间基准，不把失败响应中的文字当成指令，也不放宽证据或置信度要求。有效的 `FAIL` 或 `MANUAL_REVIEW` 不会因格式纠正而被自动改判。纠正次数和格式错误记录在阶段元数据的 `structured_output` 中，token 用量累计包含被拒绝的响应。

格式纠正与 `max_attempts` 控制的单次请求重试分别计数。最坏情况下每个提供商每次审核最多生成三次，每次请求仍受 `max_attempts` 和超时限制；调用方应保留工作流总超时。纠正耗尽后该提供商仍视为不可用，沿用现有降级规则；两个提供商都无法产出有效结果时停止审核并通知教师，工作流结束后不会额外安排自动重跑。

配置中的 `min_nonempty_lines` 会在 AI 审核之前强制执行：作业目录下的文本文件若为空文件或非空行数不足（默认 10 行，可在 `defaults` 或作业中调整，设为 0 关闭），直接判 `FAIL` 并返回 `CONTENT_TOO_SHORT`，报错信息包含文件名、实际行数和要求行数。空报告不再进入 AI 审核，避免模型因无原文可引用而反复格式失败。文本内容按需从 GitHub 拉取（与 AI 阶段相同的单文件上限），二进制文件和拉取失败的文件跳过预检，仍由图片或 AI 审核阶段处理。

所有模型都会收到审核器生成的可信时间基准：当前 UTC 时间、课程时区中的日期和年份、当前 head 的 GitHub 推送时间以及作业截止时间。同一轮审核的两个提供商、文本和图片阶段及错误说明共用一次采集的运行时间，避免跨日时不一致；时间信息也记录在结果的 `metadata.review_time` 中。年份从运行环境动态计算，不写死为某一年。延后复审不会改写原提交时间，实验记录也不要求等于复审当天或截止日。

```yaml
features:
  ai_review: true

ai:
  providers:
    - provider: glm
      model: glm-4.7-flash
    - provider: gemini
      model: gemini-3.5-flash-lite
  consensus_rounds: 3
  min_confidence: 0.8
  timeout_seconds: 60
  max_attempts: 3
  max_file_bytes: 200000
  max_total_bytes: 500000
  max_output_tokens: 2048
```

每个启用 AI 审核的作业必须在 `review_points` 中配置明确的审核点。AI 返回的问题证据必须能在对应学生文件中逐字查到；无法复核的问题按降权处理，不作为拦截依据，仅记录在阶段元数据的 `unsupported_evidence` 中。全部问题都无法复核时，原有的 `FAIL` 或 `MANUAL_REVIEW` 结论失去依据并降为 `PASS`；低于 `min_confidence` 的单模型结果记为 `MANUAL_REVIEW`，双模型的最终结论按上述复核规则处理。

复制模板填写的报告可配置 `report_template`，路径相对于课程仓库根目录，报告与模板的文件名应相同：

```yaml
assignments:
  Lab2:
    report_template: homework/Lab2/Lab2.md
    # deadline、required_files、review_points 等沿用本作业配置
```

审核器从 GitHub 返回的 PR 基础分支 SHA 读取官方模板，按行将同名报告分成 `template`（模板原文）和 `submission`（学生新增或改写）两类，完整保留原始内容。未配置模板或其他文件仍按完整原文审核；配置的模板无法读取时停止审核，不从学生分支寻找替代模板。文本和图片提示词都明确区分示例与实际结果：除非审核点要求固定值，用户名、主机名、IP、目录或日期与示例不同不能作为拒绝理由；实际记录的相互矛盾和未填写的必做项仍需检查。

`auto_merge` 需要额外的 `merge-token` 输入：GITHUB_TOKEN 无权合并 PR，会返回 `403 Resource not accessible by integration`，必须提供一个具备仓库写权限的 PAT。未配置时合并会失败，其余审核与评论功能不受影响。

需要看图才能判断的审核点应放进 `vision_review_points`。纯文本阶段只会收到 `review_points`，不会收到图片审核点，也不会收到图片文件名；视觉阶段优先使用 `vision_review_points`，未配置时回退到 `review_points`。把两类审核点混在一起会让文本阶段看到自己无法验证的条件，从而产生无法复核的 `AI_UNCERTAIN`。

启用后，学生提交中的文本/代码内容会发送至所选 API。请按学校数据规则评估是否允许，不要把教师密钥或无关仓库内容加入审核数据。

## AI 错误说明与原始审核结果

启用 `ai_feedback` 后，在审核返回 `FAIL`、`MANUAL_REVIEW` 或 `ERROR` 时增加一次独立的解释步骤。它根据全部原始问题、课程要求、当前作者的登记信息及本次 PR 的文件变更清单，归纳共同原因并提供修改建议，适用于标题、身份、目录、必交文件、超时、内容或图片审核、服务故障等不同问题。无法确定的原因应明确说明证据不足。通过、已过期的审核以及关闭评论时不调用解释模型；配置或 PR 快照无法读取时仍保留原有错误处理。

Conversation 的同一条审核评论依次展示 **AI 错误说明（辅助参考）** 和 **原始审核结果（判定依据）**，两部分均直接可见。每条原始问题编号，AI 分组必须完整对应这些编号；原始摘要、错误码、文件路径、位置、审核点和证据均保留，学生可自行对照。AI 说明仅附加在 `metadata.ai_feedback`，不能修改判定、原始问题或控制合并与关闭。

仅有标题格式或作业编号问题时，直接展示 **修改建议（规则生成）**，不调用 AI 服务。例如配置启用 `Lab2` 而标题写成 `lab2` 时，原始错误直接指出大小写差异，并按登记的学号、姓名给出可复制的完整正确标题；不会将其误报为作业未配置，也不会自动放宽大小写要求。缺少编号时展示已启用作业的标题示例，真正未配置、未启用或存在多个大小写候选时分别提示，不能擅自选择作业。即使关闭 `ai_feedback`，原始错误中的标题提示仍然保留。

仅有 `SERVICE_ERROR` 的系统错误也使用固定说明，不再调用已发生故障的模型解释故障。说明明确表示本轮已停止、未安排后续自动重跑，教师可检查原始错误并重新运行；不要求学生通过修改作业触发审核，也不提前声称邮件已发送。

```yaml
features:
  ai_feedback: true

feedback:
  timeout_seconds: 20
  max_input_bytes: 100000
  max_output_tokens: 2048
```

该功能默认关闭，与 `ai_review` 独立。按 `ai` 中的提供商顺序使用现有密钥和文本模型，每个提供商最多请求一次；首个有效解释成功后停止，不运行多轮共识。每次请求使用 `feedback.timeout_seconds`，默认最多两个提供商各 20 秒。缺少密钥、超时、解释格式错误、问题编号遗漏或输入过大时回退到原始结果，不改变审核退出码，也不会因解释失败触发教师故障邮件。评论空间不足时优先完整保留原始结果。

解释输入只包括当前作者的登记信息、课程规则、原始问题及文件路径、变更类型和 blob SHA，不下载或执行学生代码，也不发送整份名单、文件正文或截图。模型须区分新增、修改、删除、重命名以及同名但不同内容的文件，不能将目录修正等同于内容审核通过。

标题或身份检查未通过时，不向解释模型提供尚未审核的文件变更和目录状态，避免将未检查的问题混入说明。摘要、分组标题、解释和建议都只能解释本轮原始问题。

程序还会计算正确目录中已提交的文件、必交文件是否齐全，以及新增越界文件与正确位置文件的内容对应关系，明确提供给模型。正确目录已经齐全且内容相同时，说明应提示保留正确文件、移除本次新增的越界副本，避免再次建议覆盖或重命名到已有目录。

登记信息同时提供当前作者对应的 GitHub 账号及匹配结果，避免将标题中的学号、姓名错误解释成账号未登记或账号不匹配。文件存在和内容相同也不能作为内容审核合格的依据。

## 可选 PaddleOCR 与 AI 图片审核

PaddleOCR 在 GitHub Actions runner 本地运行，不需要任何 OCR 密钥。Action 仅在课程配置实际启用 `ocr_review` 时安装 `paddleocr==3.7.0` 和 `paddlepaddle==3.3.1`。首次运行会下载所配置的 OCR 模型。

默认使用体积与准确率较平衡的 `PP-OCRv6_small_det` 和 `PP-OCRv6_small_rec`。OCR 只负责提取截图文字；清洗后的图片和 OCR 结果一起交给所选多模态模型判断。`ocr_review: false` 时不安装 PaddleOCR，GLM 与 Gemini 直接读取图片。

```yaml
features:
  ocr_review: true
  vision_review: true

ocr:
  detection_model: PP-OCRv6_small_det
  recognition_model: PP-OCRv6_small_rec
  min_line_confidence: 0.65
  max_text_chars: 20000

vision:
  providers:
    - provider: glm
      model: glm-4.6v-flash
    - provider: gemini
      model: gemini-3.5-flash-lite
  consensus_rounds: 3
  min_confidence: 0.85
  fail_confidence: 0.9
  max_images: 6
  max_image_bytes: 5000000
  max_total_bytes: 12000000
  max_pixels: 25000000
  max_side: 4096

assignments:
  Lab1:
    vision_files:
      - result.*
```

`ocr_review` 必须与 `vision_review` 一起启用。审核器只读取当前 PR head 中精确 blob SHA 对应的图片；图片会先检查格式、像素数、多帧、解压炸弹和大小，再统一转成不含元数据的 PNG。AI 只收到作业内的相对图片文件名，不收到学号或姓名。

图片 `FAIL` 使用更高的 `fail_confidence` 阈值。OCR 类问题的证据还必须能在 PaddleOCR 结果中逐字复核，否则降级为 `MANUAL_REVIEW`。双模型的临时故障按共识规则降级；两个审核通道均不可用时返回 `ERROR`。

GLM 和 Gemini 的文本、图片审核分别共用自己的 API Key。密钥只保存为课程仓库的 GitHub Actions Secret：

```bash
gh secret set GLM_API_KEY
gh secret set GEMINI_API_KEY
```

命令会交互式读取密钥，不要把密钥写在命令行、YAML、代码、PR 或日志中。PaddleOCR 不需要密钥，`PAT_TOKEN` 也不需要。

## 账号与标题规则

审核器先根据 PR 作者查询 `students.yml`，然后生成唯一预期标题：

```text
[{student_id}{student_name}]{assignment_id}作业提交
```

例如：

```text
[2023010102刘西莹]Lab1作业提交
```

- 未登记账号：`MANUAL_REVIEW`
- 登记账号冒用其他学生身份：`FAIL`
- 标题、目录和作业编号不一致：`FAIL`

## 在课程仓库中调用

课程仓库应固定到审核器的 commit SHA，并授予评论、合并和关闭所需的 `pull-requests: write` 与 `contents: write`：

```yaml
- name: Run course reviewer
  uses: LiuXiYing/course-pr-reviewer@REVIEWER_COMMIT_SHA
  with:
    config-path: .github/course-review.yml
    students-path: .github/students.yml
    metadata-dir: pr-info
    github-token: ${{ secrets.GITHUB_TOKEN }}
    merge-token: ${{ secrets.PAT_TOKEN }}
    glm-api-key: ${{ secrets.GLM_API_KEY }}
    gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
    teacher-email: teacher@example.com
    smtp-username: sender@gmail.com
    smtp-password: ${{ secrets.GMAIL_APP_PASSWORD }}
```

`teacher-email` 配置后，审核结果为 `MANUAL_REVIEW` 或 `ERROR` 时会通过
SMTP SSL 通知任课教师，并在 PR Conversation 中确认邮件已发送。`PASS`、
`FAIL` 和正常自动合并不会发送邮件。使用 Gmail 时，应启用两步验证并将
应用专用密码保存为仓库 Secret；不要把密码写入工作流或课程配置文件。

安全调用分为两个工作流：

1. [`examples/caller-collect.yml`](examples/caller-collect.yml) 使用受信任默认分支上的 `pull_request_target` 读取事件元数据，不检出或执行学生代码，fork PR 无需逐次审批。
2. [`examples/caller-review.yml`](examples/caller-review.yml) 在 `workflow_run` 中检出默认分支、重新读取 GitHub API 的当前 PR 数据并运行共享审核器。

高权限阶段不检出、导入或执行学生 PR 内的任何代码。不要在正式课程中引用可移动的 `main` 标签。

## 开发

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI 在 Python 3.11 和 3.12 上运行全部测试，并验证示例配置。

## 后续阶段

1. 将当前结构化运行元数据汇总成学期统计报告。
