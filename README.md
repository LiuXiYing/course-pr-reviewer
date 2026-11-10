# Course PR Reviewer

面向课程作业仓库的通用 GitHub PR 审核器。每个课程只维护作业配置和学生名单，身份校验、确定性规则、AI、OCR 和视觉审核由一个版本化 Action 复用。

> 当前状态：`0.15.0`。确定性规则、Gemini/GLM 双模型共识审核、可选本地 PaddleOCR、AI 错误说明、PR 评论、自动合并、超期关闭和重复提交关闭均已实现；本版新增 `legacy` / `tiered` 迟交计分、48 小时修正期、`grades` 分支成绩事实与 CSV 导出。

## 设计原则

- 学生身份来自经过教师确认的 GitHub 账号映射，不能由 PR 标题冒充。
- PR 标题仍然严格校验，便于教师直接查看。
- 只有 `PASS` 可以自动合并；不确定结果进入 `MANUAL_REVIEW`。
- 双模型均不可用、GitHub、OCR 或配置异常必须失败关闭；单模型临时不可用时显式降级并记录。
- 审核哪个 head SHA，就只能合并哪个 SHA。
- 每次审核都会发布一条新评论，使学生能在 Conversation 底部直接看到最新结果；评论末尾标明对应的 head SHA。
- 截止时间使用当前 head 提交被推送到 PR 的服务器时间（由调用方工作流写入 `event_at`），而不是 PR 首次创建时间，避免截止前开占位 PR、截止后补交；达到关闭阈值后自动评论并关闭。
- 截止时间判定排在内容审核之后：超时不再短路 AI 与图片审核，内容问题照常返回，超时只作为附加结论跟在后面；内容全部通过时才单独说明本次提交只是因为超时。关闭只在最终决策为 `FAIL` 时请求，`MANUAL_REVIEW` 与 `ERROR` 一律不关闭。标题里认不出作业编号时拿不到 `deadline`，不做时间判定。
- 标题里的作业编号确认后、进入内容审核之前，先查目标分支上是否已有这个学生这个作业的提交目录：已有说明该作业此前已经合并，判为重复提交，评论里直接写清「该作业已合并」并关闭 PR，不调用 AI，也不看文件和截止时间。这一步只认标题里已确认的编号，标题不合规的 PR 走不到这里；查不到（缺 GitHub 客户端或基础分支 SHA）或查询失败时不下结论，交回正常审核。重复提交的关闭是课程既定规则，不受 `close_late_pr` 开关影响。
- 共享审核器不保存课程密钥、学生名单或课程题目。
- 高权限工作流永远不检出或执行学生 PR 中的代码。
- 内容验收与时间计分分开：`review_decision` 只回答内容是否合格，`grade_decision` 只回答值多少分。启用 `grading` 后，迟交不再阻止合并，只影响成绩上限（详见下文「迟交计分与 grades 分支」）。
- 成绩只接受可信输入：默认分支的配置、GitHub 服务端的 head 推送时间、已验证的审核结论和确实合并成功的 PR。提交信息里的作者时间、PR 评论、学生填写的分数一律不参与计算。
- 分档边界按秒比较、用 `Decimal` 计算，边界值 `48 小时整` 属于 90% 档，`48 小时零 1 秒` 才进入 75% 档，不存在 48～72 小时的空档。

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
├── students.yml
└── workflows/
    ├── pr-check.yml          # 可信事件元数据
    ├── pr-review.yml         # 审核 + 合并 + 写收据
    ├── grade-reconcile.yml   # 每日对账
    └── grade-export.yml      # 手工导出 CSV
```

`course-review.yml` 集中保存整个学期的作业配置，每次发布新作业时向 `assignments` 追加一项。`students.yml` 在开学时根据教师确认的 Excel 名单生成，学期中仅在账号变更时更新。

完整示例见：

- [`examples/course-review.yml`](examples/course-review.yml)
- [`examples/students.yml`](examples/students.yml)

新作业可选择启用 [Markdown 行数与字符数量检查（第一阶段）](docs/template-metrics.md)，
配置示例见 [`examples/template-metrics.yml`](examples/template-metrics.yml)。
该功能仅在具体作业的 `template_metrics.enabled: true` 时执行；旧课程无需改 YAML。

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
| `PASS` | 全部检查通过，含达到复核上限后至少一方 PASS 的分歧放行 | 是 |
| `FAIL` | 存在有证据的明确违规 | 否 |
| `MANUAL_REVIEW` | 身份未登记、内容有歧义或模型不确定 | 否 |
| `ERROR` | GitHub、配置或外部服务异常 | 否 |

稳定的原因代码定义在 [`models.py`](src/course_pr_reviewer/models.py)，所有运行结果遵守 `review-result.schema.json`。

## Gemini 与 GLM 双模型审核

文本和图片阶段都可以配置 GLM 与 Gemini 两个提供商。两个模型每轮独立审核；结论一致时立即按该结论处理（均通过则通过，均拒绝则合并可复核的问题，均不确定则转人工）。结论不一致时，将上一轮的意见和证据作为线索重新核对原始材料，默认最多审核三轮；达到上限仍有分歧时，只有至少一方明确 PASS 才按规则放行，双方都不是 PASS 时不自动合并（有 FAIL 按 FAIL 处理，否则转人工复核）。每轮的双方结论、理由和证据保存在阶段元数据的 `consensus.history[].findings` 中。一个模型临时不可用时采用另一个模型的结果并在 PR 评论中标记降级状态，两个模型均不可用时暂停合并。

`consensus_rounds` 控制最多审核几轮，双模型默认 `3`，可配置 `1` 至 `3`；单模型必须为 `1`。PR 评论分别显示实际次数与上限，例如“已审核 1 轮，最多 3 轮”；达到上限仍有分歧时，明确标注是“至少一方 PASS，按规则通过”还是“无 PASS，不自动合并”。分歧放行仅作用于该 AI 阶段，账号、文件、截止时间、其他启用的审核阶段及合并前 head SHA 校验仍须通过。

缺少密钥、鉴权失败或模型名配置错误属于配置故障，不允许降级为单模型。API 超时、429 或服务端临时错误在内部重试耗尽后才属于可降级故障。

文本和图片审核遇到无效 JSON、缺少字段、空证据等输出格式错误时，会将具体字段和 Schema 约束反馈给原模型，最多自动纠正两次（加上首次生成，共三次）。每次仍使用相同的原始提交、审核规则和时间基准，不把失败响应中的文字当成指令，也不放宽证据或置信度要求。有效的 `FAIL` 或 `MANUAL_REVIEW` 不会因格式纠正而被自动改判。纠正次数和格式错误记录在阶段元数据的 `structured_output` 中，token 用量累计包含被拒绝的响应。

每条 `evidence` 最多允许 2000 个字符，以容纳需要连续引用的表格或终端输出；提示词建议通常只引用 100 至 300 字符的必要证据。审核器不会自动截短证据或删除问题，超出字段上限仍需重新生成，文本证据仍须在原文中核对。字段的 `maxLength` 是字符限制，与整次请求的 `max_output_tokens` 不同，单独提高 token 上限不能修复字段超长错误。

API 返回 `finish_reason=length` 时，即使 JSON 看起来完整，也按输出被 token 上限截断处理并尝试纠正，防止接受不完整的审核。日志会明确记录截断原因；成功响应的 `finish_reason`（提供商有返回时）保存在阶段元数据中。纠正期间保持配置的 token 上限，持续截断时可由教师调整相应阶段的 `max_output_tokens`。

格式纠正与 `max_attempts` 控制的单次请求重试分别计数。最坏情况下每个提供商每次审核最多生成三次，每次请求仍受 `max_attempts` 和超时限制；调用方应保留工作流总超时。纠正耗尽后该提供商仍视为不可用，沿用现有降级规则；两个提供商都无法产出有效结果时停止审核并在 PR 评论和 Actions 日志中记录错误，工作流结束后不会额外安排自动重跑。

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

模板必须是非空 UTF-8 普通文件。完整分段比对禁用忽略高频行的启发式，模板行数与报告行数的乘积上限为 4,000,000，超过时转人工审核。官方模板明确标为“示例／例如”的原文片段，以及明确介绍为示例的代码块，会额外标记 `is_example`；若模型证据仅引用这些复制的示例，且未出现在其他实际内容中，该问题不作为拒绝依据，保存在 `template_example_issues` 中。未填写的必做记录和学生实际填写仍可作为违规证据。

Markdown 观察表中的必填地址可使用 `required_address_cells` 做程序检查，避免模型漏掉空白或“（填写）”等占位内容：

```yaml
assignments:
  Lab5:
    required_address_cells:
      - file: Lab5.md
        section: "3.6.4"
        row: "当前 data 地址（图 C 为 v.data）"
        columns: [1, 2, 3]
      - file: Lab5.md
        section: "3.6.4"
        row: "newData 地址"
        columns: [2]
    # deadline、required_files、review_points 等沿用本作业配置
```

`file` 相对于学生作业目录，必须是 `required_files` 中的必交 Markdown 文件；`section` 匹配标题编号或完整标题；`row` 匹配表格第一列，忽略空白和 Markdown 强调、代码标记。`columns` 从行标题后的第一个数据列起按 1 计数，只检查列出的单元格，因此明确标注“不填写”的其他格子可保留原文。检查接受十六进制地址、可选的 `0x` 前缀和 Markdown 包裹，不比较地址的具体数值。只读取指定章节中的表格，不以其他章节、代码块或 HTML 注释中的示例代替答案。缺少记录行或未填写地址会在 AI 阶段之前返回 `REQUIRED_ADDRESS_MISSING`；文件读取失败会停止审核。该检查与 `min_nonempty_lines` 独立，未配置的作业保持原有行为。

`auto_merge` 需要额外的 `merge-token` 输入：GITHUB_TOKEN 无权合并 PR，会返回 `403 Resource not accessible by integration`，必须提供一个具备仓库写权限的 PAT。未配置时合并会失败，其余审核与评论功能不受影响。

需要看图才能判断的审核点应放进 `vision_review_points`。纯文本阶段只会收到 `review_points`，不会收到图片审核点，也不会收到图片文件名；视觉阶段优先使用 `vision_review_points`，未配置时回退到 `review_points`。把两类审核点混在一起会让文本阶段看到自己无法验证的条件，从而产生无法复核的 `AI_UNCERTAIN`。

启用后，学生提交中的文本/代码内容会发送至所选 API。请按学校数据规则评估是否允许，不要把教师密钥或无关仓库内容加入审核数据。

## AI 错误说明与原始审核结果

启用 `ai_feedback` 后，在审核返回 `FAIL`、`MANUAL_REVIEW` 或 `ERROR` 时增加一次独立的解释步骤。它根据全部原始问题、课程要求、当前作者的登记信息及本次 PR 的文件变更清单，归纳共同原因并提供修改建议，适用于标题、身份、目录、必交文件、超时、内容或图片审核、服务故障等不同问题。无法确定的原因应明确说明证据不足。通过、已过期的审核以及关闭评论时不调用解释模型；配置或 PR 快照无法读取时仍保留原有错误处理。

Conversation 的同一条审核评论依次展示 **AI 错误说明（辅助参考）** 和 **原始审核结果（判定依据）**，两部分均直接可见。每条原始问题编号，AI 分组必须完整对应这些编号；原始摘要、错误码、文件路径、位置、审核点和证据均保留，学生可自行对照。AI 说明仅附加在 `metadata.ai_feedback`，不能修改判定、原始问题或控制合并与关闭。

仅有标题格式或作业编号问题时，直接展示 **修改建议（规则生成）**，不调用 AI 服务。例如配置启用 `Lab2` 而标题写成 `lab2` 时，原始错误直接指出大小写差异，并按登记的学号、姓名给出可复制的完整正确标题；不会将其误报为作业未配置，也不会自动放宽大小写要求。缺少编号时展示已启用作业的标题示例，真正未配置、未启用或存在多个大小写候选时分别提示，不能擅自选择作业。即使关闭 `ai_feedback`，原始错误中的标题提示仍然保留。

仅有 `SERVICE_ERROR` 的系统错误也使用固定说明，不再调用已发生故障的模型解释故障。说明明确表示本轮已停止、未安排后续自动重跑，教师可检查原始错误并重新运行；不要求学生通过修改作业触发审核，邮件通知已停用。

```yaml
features:
  ai_feedback: true

feedback:
  timeout_seconds: 20
  max_input_bytes: 100000
  max_output_tokens: 2048
```

该功能默认关闭，与 `ai_review` 独立。按 `ai` 中的提供商顺序使用现有密钥和文本模型，每个提供商最多请求一次；首个有效解释成功后停止，不运行多轮共识。每次请求使用 `feedback.timeout_seconds`，默认最多两个提供商各 20 秒。缺少密钥、超时、解释格式错误、问题编号遗漏或输入过大时回退到原始结果，不改变审核退出码。评论空间不足时优先完整保留原始结果。

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
```

邮件通知已全面停用：`PASS`、`FAIL`、`MANUAL_REVIEW`、`ERROR` 均不会发送邮件，
配置错误、API 故障和其他异常也不会触发邮件。审核结果继续通过 PR 评论和 Actions 日志查看。
旧工作流中的 `teacher-email` 与所有 `smtp-*` 输入仅为兼容而保留，值会被忽略，
无需配置邮箱或 SMTP 密码。旧版 `notify` 命令固定返回 `{"email":"disabled"}` 和退出码 0，
不读取邮件凭据、不修改审核结果，也不因配置或结果文件缺失而报邮件错误。

安全调用分为两个工作流：

1. [`examples/caller-collect.yml`](examples/caller-collect.yml) 使用受信任默认分支上的 `pull_request_target` 读取事件元数据，不检出或执行学生代码，fork PR 无需逐次审批。
2. [`examples/caller-review.yml`](examples/caller-review.yml) 在 `workflow_run` 中检出默认分支、重新读取 GitHub API 的当前 PR 数据并运行共享审核器。

高权限阶段不检出、导入或执行学生 PR 内的任何代码。不要在正式课程中引用可移动的 `main` 标签。

## 迟交计分与 grades 分支

### 两类作业

| 类型 | 适用范围 | 是否分段扣分 | 逾期能否合并 | 写入成绩 |
|---|---|---|---|---|
| `legacy` | 制度上线前已发布的作业 | 否 | 能 | 记录完成事实，`awarded_points` 为 `null` |
| `tiered` | 上线后新发布的作业 | 是 | 能 | 记录分数与迟交依据 |

`legacy` 作业不会因为超时被关闭，也不会被强行赋成 100 分或 70 分；只有显式配置 `legacy.fixed_points` 才会给固定分值。

### 分档（以超出 deadline 的秒数判定）

| 迟交区间 | 上限 | 10 分作业 |
|---|---:|---:|
| `<= deadline` | 100% | 10.00 |
| `0 < 秒数 <= 48h` | 90% | 9.00 |
| `48h < 秒数 <= 168h` | 75% | 7.50 |
| `168h < 秒数 <= 336h` | 60% | 6.00 |
| `336h < 秒数 <= 672h` | 40% | 4.00 |
| `> 672h` 且未过最终收件截止 | 20% | 2.00 |
| 晚于 `course.final_accept_at` | 0% | 不计分，默认不合并 |

`deadline` 是精确时刻：`2026-11-21T00:00:00+08:00` 表示 11 月 20 日 24:00，不是 21 日全天。

### 48 小时修正期

截止前用同一个 head 推过一次**实质性**内容的学生，如果在 `deadline + 48h` 内改到通过，按 100% 计分（`ON_TIME_CORRECTED`）。实质性由确定性规则判断：必交文件齐全、文本达到行数要求且不是模板复制、图片不是空白/占位/过小、没有删改他人或旧作业文件。空 PR 占位、截止后才上传内容、只改姓名、只贴空白截图都拿不到资格。判断依据是当时的 head SHA 与 GitHub 服务端事件时间，事后再改标题、关闭重开或重跑 Action 都不会产生新的提交时间。

无法核验时记为 `ELIGIBILITY_UNKNOWN`，不发放资格、按真实迟交档计分，并在评论里说明可联系教师复核。

### grades 分支

每个课程仓库有自己的 `grades` 分支，从空的初始提交创建，不放在 `main`：

```text
metadata.json           课程标识与 schema 版本
claims/Lab9/<学号>.json  截止前有效提交（最早一条生效）
receipts/Lab9/<学号>.json 最终验收与得分事实
overrides/Lab9/<学号>.json 教师审批的例外（不改写收据）
audit/YYYY-MM/*.json    覆盖与对账审计
reports/grades.csv      可按收据重建的汇总表
reports/submissions.csv 每次作业明细
reports/pending.csv     尚未确认合并的记录
reports/summary.json    完成率统计
```

写收据分两步：审核 `PASS` 时先写 `PENDING_ACCEPTANCE`，合并成功并核对 merge commit 后才写 `FINALIZED`。tiered 作业如果先写收据失败，就不合并；已合并但收据没写完，由每日 `grade-reconcile` 补齐，不要求学生重新推送。

评论只有一条：合并确认后，审核器会把同一条评论刷新为最终状态（记录已写入 + 按合并后事实重算的累计得分率），不会另发重复评论；刷新失败不影响合并与收据，只在结果里记 `comment_update: failed`。

`reports/` 不在合并路径里重建：一次重建要把所有收据读一遍，几十人一个学期就是几百次 API 请求，会吃掉 `GITHUB_TOKEN` 的每小时额度。由每日 `grade-reconcile`（发现报表落后于收据时）或手工 `grade export` 重建，需要合并即刷新时把 `grading.storage.export_on_finalize` 设为 `true`。

**公开仓库的 `grades` 分支同样是公开的。** 默认记录里不放姓名、联系方式或申诉材料；如果学校不允许公开可识别的分数，请把 `student_id` 换成不可逆代号，或改用私有仓库。

### 教师侧命令

```bash
# 校验 grading 配置（不访问任何分支）
course-pr-reviewer grade validate --config .github/course-review.yml --students .github/students.yml

# 初始化 grades 分支（GitHub 上创建 orphan 分支；--dir 可先写成本地目录）
course-pr-reviewer grade init --config .github/course-review.yml --students .github/students.yml

# 重建 reports/（可随时删除后重跑）
course-pr-reviewer grade export --config .github/course-review.yml --students .github/students.yml

# 对账：把已确认合并的 PENDING 收据升级为 FINALIZED
course-pr-reviewer grade reconcile --config .github/course-review.yml --students .github/students.yml --since 2026-11-01T00:00:00+08:00
```

以上命令都支持 `--dry-run`（不写入任何内容）。`grade reconcile` 额外支持 `--only-pr <n>`。

### 课程仓库需要新增的两个工作流

```yaml
# .github/workflows/grade-reconcile.yml
name: 成绩对账
on:
  schedule: [{ cron: "17 20 * * *" }]   # 每天北京时间 04:17
  workflow_dispatch:
    inputs:
      since: { description: 起始时间（ISO，可留空）, required: false }
      only_pr: { description: 只处理这个 PR, required: false }
permissions: { contents: write, pull-requests: read }
concurrency: { group: grade-write-${{ github.repository }}, cancel-in-progress: false, queue: max }
jobs:
  reconcile:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
        with: { persist-credentials: false }
      - uses: LiuXiYing/course-pr-reviewer@REVIEWER_COMMIT_SHA
        with:
          mode: grade-reconcile
          config-path: .github/course-review.yml
          students-path: .github/students.yml
          github-token: ${{ secrets.GITHUB_TOKEN }}
          since: ${{ inputs.since }}
          only-pr: ${{ inputs.only_pr }}
```

```yaml
# .github/workflows/grade-export.yml
name: 成绩导出
on:
  workflow_dispatch:
    inputs:
      dry_run: { description: 只打印计划, required: false, default: "false" }
permissions: { contents: write }
jobs:
  export:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
        with: { persist-credentials: false }
      - uses: LiuXiYing/course-pr-reviewer@REVIEWER_COMMIT_SHA
        with:
          mode: grade-export
          config-path: .github/course-review.yml
          students-path: .github/students.yml
          github-token: ${{ secrets.GITHUB_TOKEN }}
          dry-run: ${{ inputs.dry_run }}
```

用 `GITHUB_TOKEN` 写 `grades` 分支不会再触发其它 `push` 工作流，所以报表由这两个工作流显式重建，不依赖 push 事件。

### 迁移步骤

1. 在 `course` 段增加稳定的 `id`（小写连字符）。
2. `features` 增加 `grading: true`，并把 `close_late_pr` 设为 `false`。
3. 增加 `grading` 块（照抄 [`examples/grading-review.yml`](examples/grading-review.yml)）。
4. 给**每一个已发布**的作业加 `grading: { mode: legacy }`，不要凭 deadline 是否过去推测。
5. 下一次发布新作业时用 `grading: { mode: tiered, max_points: "10.00" }`。
6. 运行 `grade validate`，再运行 `grade init` 创建 `grades` 分支。
7. 先在一个仓库灰度 2～3 个真实 PR（准时、迟交、修正），核对评论、合并与 `reports/grades.csv` 后再推广。

## 开发

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

CI 在 Python 3.11 和 3.12 上运行全部测试，并验证示例配置。

## 后续阶段

1. 教师专用覆盖工作流（`overrides/` 已有 schema 与导出层，尚无表单界面）。
2. 可选的 rubric 部分分：当前 `PASS/FAIL` 只决定是否计分，不把 AI 置信度当百分制。
