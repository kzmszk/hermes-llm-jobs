# Hermes LLM Jobs

複数アプリからのLLM処理を受け付ける、独立した非同期ジョブサービスです。

- API: https://hermes-llm-jobs.kazumasa.workers.dev
- Cloudflare Worker: `hermes-llm-jobs`
- D1: `hermes-llm-jobs`（クイズのDBとは別）
- 実行: PC上のHermes Agent / GPT-6 Luna / Codexログイン

```mermaid
flowchart LR
  Quiz[クイズWorkerと専用DB] -->|依頼を登録・結果を取得| API[共通ジョブWorker]
  Other[他アプリのWorker] -->|依頼を登録・結果を取得| API
  API <--> DB[(共通ジョブ専用D1)]
  Cron[Hermes cron / 3分ごと] --> Tick[dispatcher.py tick]
  Tick -->|最大1件を確保| API
  Tick --> Local[(PC SQLite / jobs + outbox)]
  Tick --> Common[独立 common worker / Hermes / GPT-6 Luna]
  Tick --> Art[独立 illustration worker / Claude Code / Opus]
  Common --> Local
  Art --> Local
  Local -->|保存済み結果を再送| API
```

## アプリから使う

アプリごとに別のBearerキーを発行し、バックエンドのSecretとして保持します。

```bash
npm ci
python3 provision-app.py my-app --types document.summarize
```

キーは `.private/apps/my-app.json` へ所有者限定で保存され、画面には表示しません。既存アプリIDで再実行するとキーをローテーションします。必要なイベントだけを許可してください。

### 登録

`POST /api/jobs` に `Authorization: Bearer <アプリ用キー>` と `Content-Type: application/json` を付けます。

```json
{
  "type": "document.summarize",
  "version": 1,
  "idempotencyKey": "document-123-v1",
  "payload": {"text": "要約する文章"}
}
```

応答の `id` を保存し、同じアプリ用キーで `GET /api/jobs/{id}` を呼びます。
状態は `pending` / `running` / `completed` / `failed`。完了時の `result` に結果とモデル・指示の版が入ります。他アプリのジョブは取得できません。同じアプリ・同じ重複防止キーで同じ入力を再送すると同じID、入力が違うと409です。

| イベント | payload | result |
|---|---|---|
| quiz.grade / v1 | question, modelAnswer, rubric（3項目）, answer | criteria（各0〜2点と理由）, confidence, feedback |
| document.summarize / v1 | text（最大12,000文字） | summary, keyPoints |
| illustration.svg / v1 | subject（最大60文字、記号 `<>{}` などなし） | svg（線画のSVG、24,000文字まで）, memo（描き手の設計メモ、任意） |
| video.generate / v1 | theme（最大60文字、記号 `<>{}` などなし）, minutes（2 か 3）, notes（任意、最大1,500文字） | title, subtitle, seconds, width, height, style, chapters（2〜16個）, video / poster（R2 のキー）, bytes, qa（任意） |

HTTP本文は最大32KiBです。採点基準の文字列などにも個別上限があります。APIの入力・結果形式を `worker.js`、Hermesへの指示・結果検証を `runner.py` で管理しています。任意のコマンド・URL・プロンプトをイベント入力で実行する機能はありません。

## イラスト（illustration.svg）

題材のことばから、プロンプト [prompts/illustration-v3.md](prompts/illustration-v3.md) で線画のSVGを1枚描きます。スケッチブック（demos の sketchbook）の「おためし」ページが使っています。

- 実行は別レーンです。`dispatcher.py tick` が確保してSQLiteに保存したあと、`worker --lane illustration` を独立プロセスで起動します。common workerと別のファイルロックを持ち、採点・要約を待たせません。取得cronは共通の1本だけです。旧 `runner.py --illustration` は移行・ロールバック用に変更せず残しています。
- 描くのは Opus（`claude-opus-5-5`）です。runner がこのPCの Claude Code CLI を `claude -p --tools "" --no-session-persistence` で直接呼びます（ツールなし、空の作業ディレクトリ、HOME/PATH/LANG だけの環境）。CLI に一度 `/login` しておく必要があります。Luna は経由しません。
- 題材は訪問者が入力した信頼できない文字列として、固定のプロンプトに差し込むだけです。結果のSVGは [svgclean.py](svgclean.py) の許可リスト（要素・属性・`url(#id)` のみ）で作り直し、先頭のコメントを memo として切り出します。Worker でも最後に危険な記述がないか確かめます。
- 処理権の期限は1,200秒（ほかは600秒）、Claude の呼び出しは600秒で打ち切ります。1アプリあたり1日30件まで（UTC日、日本時間9時に戻る）。同じ重複防止キーの再送は数えません。
- 1枚あたり3分ほど、Claude の利用枠を使います（API換算で0.4ドル前後）。

## 動画（video.generate）

テーマと長さ（2分か3分）、使ってほしい事実（任意）から、二人の掛け合いの解説動画を1本つくります。demos の `auto_movie` ページの「つくる」が使っています。つくるのは [demos/auto_movie](../demos/auto_movie)（Claude が台本と絵を書き、VOICEVOX が読み上げ、HyperFrames が書き出す）で、ふつう10〜15分、Claude の利用枠を API 換算で1.6ドルほど使います。

- 実行は3本目のレーン `video` です（専用のファイルロック、cron は共通の1本のまま）。`runner.make_video` が `node auto_movie/bin/auto-movie.mjs job --input <一時ファイル> --id <ジョブID>` を起動します。訪問者の文字列は**JSONファイルで渡し、コマンドライン引数には出しません**。環境変数は許可リスト（HOME、PATH など）だけです。ログは `.private/video-logs/<ジョブID>.log`（所有者限定）。
- auto_movie は入力を再検査し、絵を許可リストで作り直し、完成した軽量版（1080p）とポスターを R2 のバケット `demos-media` の `movies/<ジョブID>/video.mp4` と `poster.webp` に上げます。**結果の JSON にはこの2つのキーだけが入り、ブローカーも runner も「そのジョブ自身のID」以外を含む結果を拒否します**（他のジョブや任意の URL は指せません）。動画のファイルそのものは D1 に入りません。
- 処理権の期限は2,700秒、runner の打ち切りは2,400秒（`VIDEO_TIMEOUT`）。1アプリあたり1日3本まで（UTC日、日本時間9時に戻る）。同じ重複防止キーの再送は数えません。失敗の再試行（最大3回）では、実行ディレクトリに残した台本・絵・LLM の応答が使われるので、やり直しの費用はほとんどかかりません。
- テーマが不適切だとモデルが判断すると `job` が終了コード3で終わり、runner は `invalid_result` として失敗にします。入力が不正なら終了コード2で、同じく `invalid_result` です（再試行しても変わりません）。
- 動作は `AUTO_MOVIE_DIR` で別の場所に向けられます（既定は `~/work/demos/auto_movie`、そこでチェックアウトされているブランチのコードが動きます）。auto_movie 側は R2 へのアップロードに、demos リポジトリの `wrangler` のログインを使います。
- 確認は `node smoke-video.mjs`。使い捨てのローカルDBとローカルのブローカーを自分で立てて、登録・重複・上限・処理権・結果の検証・他アプリからの閲覧拒否・失敗の差し戻し、そして実物の dispatcher と worker（auto_movie の代役つき）までを通します。本番には触りません。

## 新しい処理の種類を追加する手順

ここでは `document.translate`（文章を英語・日本語へ翻訳する処理）を追加する例で説明します。**以下は追加する際の手順とコード例です。現在のサービスには翻訳処理はまだ実装されていません。**

既存の `quiz.grade` や `document.summarize` をもう1件依頼するだけなら、コード変更は不要です。「アプリから使う」の登録APIを使ってください。

### 1. 入力と結果の形式を決める

登録するジョブの例:

```json
{
  "type": "document.translate",
  "version": 1,
  "idempotencyKey": "translate-document-123-en-revision-1",
  "payload": {
    "text": "こんにちは。",
    "targetLanguage": "en"
  }
}
```

LLMから受け取る結果の例:

```json
{"translation":"Hello."}
```

この例では入力本文は最大4,000文字、翻訳結果は最大8,000文字、翻訳先は `ja` / `en` のみとします。モデル名と指示の版はrunnerが別途付けるので、LLMの出力には含めません。

`idempotencyKey` はアプリ内で一意にします。同じ依頼の通信再試行には同じ値を使い、原文・翻訳先・版を変えた依頼には別の値を使ってください。種類が違っても同じアプリ内でキーを使い回すと衝突します。

### 2. Workerに種類と入力・結果の検証を追加する

[worker.js](worker.js) の `TYPES` に `document.translate` を追加します。

```js
const TYPES = ['quiz.grade', 'document.summarize', 'document.translate'];
```

`input(type, p)` の `quiz.grade` 分岐の後、既存の要約用検証の**前**に追加します。

```js
if (type === 'document.translate') {
  if (!str(p.text, 4000) || !['ja', 'en'].includes(p.targetLanguage)) {
    fail(400, 'text（最大4,000文字）とtargetLanguage（ja/en）が必要です。');
  }
  return { text: p.text, targetLanguage: p.targetLanguage };
}
```

`output(type, r)` も、既存の要約用検証の**前**に追加します。

```js
if (type === 'document.translate') {
  if (!str(r.translation, 8000)) fail(400, '翻訳結果の形式が不正です。');
  return { translation: r.translation };
}
```

現状は採点以外が要約の検証に進む構造です。`TYPES` への追加だけでは動かないので、入力と結果の両方に分岐を追加してください。HTTP本文の上限32KiBも適用されます。

### 3. PCのrunnerに処理を追加する

[runner.py](runner.py) で、次の3か所を変更します。

**取得対象の `TYPES` に追加:**

```python
TYPES = ['quiz.grade', 'document.summarize', 'document.translate']
```

**`validate(kind, result)` の要約用検証より前に追加:**

```python
    if kind == 'document.translate':
        if not text(result.get('translation'), 8000):
            raise ValueError('invalid_result')
        return {'translation': result['translation']}
```

**`run_model(job)` の採点用 `if` と要約用 `else` の間に追加:**

```python
    elif job['type'] == 'document.translate':
        language = {'ja': '日本語', 'en': '英語'}[job['payload']['targetLanguage']]
        rules += (
            f'textを{language}に翻訳してください。原文の意味を保ち、説明を加えないでください。'
            '形式は {"translation":"翻訳した文章（8000文字以内）"}。'
        )
```

既存の共通指示（入力内の命令を実行しない・ツールを使わない）は残します。呼び出し元から任意のプロンプトや実行コマンドを渡す設計にはしません。

Workerとrunnerの結果形式・上限を揃えます。JavaScriptの文字列長はUTF-16単位、PythonはUnicodeコードポイント単位なので、絵文字などを含む上限付近の文字列ではWorker側がより厳しくなる場合があります。

指示を変更した記録として、`VERSION` も例えば `hermes-jobs-v2` に更新します。現在この値は全種類共通です。`hermes-call.py` の変更は不要です。

### 4. キー発行コマンドの許可リストを更新する

[provision-app.py](provision-app.py) の `--types` に指定している `choices` も追加します。

```python
choices=['quiz.grade', 'document.summarize', 'document.translate']
```

更新箇所の一覧:

| ファイル | 変更する場所 |
|---|---|
| `worker.js` | `TYPES`、`input()`、`output()` |
| `runner.py` | `TYPES`、`validate()`、`run_model()`、`VERSION` |
| `provision-app.py` | `--types` の `choices` |
| `README.md` | 対応イベント表と入力・結果の説明 |

ジョブの入力・結果はJSONとして既存テーブルに保存されるため、この例ではDBマイグレーションは不要です。現在の登録・取得処理は `version: 1` を前提としており、`version: 2` に変える場合はAPIとrunnerの対応も別途必要です。

### 5. WorkerとPC側の変更を反映する

このPCでは次の順番で反映します。作業途中のrunnerが動かないよう、コードを編集し始める前にジョブを停止してください。

```bash
cd /home/kazu/work/hermes-llm-jobs
hermes cron pause 6df0ad98fae9
# この間に手順2〜4の編集を行う
npm ci
npm run deploy
# デプロイ成功後に再開
hermes cron resume 6df0ad98fae9
```

停止前に起動済みのrunnerは、その処理が終了するまで待ってから編集します。停止中もAPIへの登録はでき、未処理ジョブはDBに残ります。デプロイに失敗した場合は、新しい種類を取得するrunnerをそのまま再開せず、Workerとの対応を揃えてください。

Hermes cronは毎回このディレクトリの `dispatcher.py tick` を起動し、独立workerが `runner.py` のモデル関数を利用します。次のworkerから変更が読み込まれます。新しい処理のために別の定期ジョブを作る必要はありません。別PCで実行する場合は、そのPCにも同じ変更を配布します。

### 6. 利用するアプリに許可する

新しいアプリの例:

```bash
python3 provision-app.py translation-app --types document.translate
```

既存アプリに追加する場合は、**引き続き使う種類をすべて指定**します。

```bash
python3 provision-app.py my-app --types document.summarize document.translate
```

このコマンドは種類の追加だけでなく、**そのアプリのキーも更新し、許可リストを置き換えます。** 以前のキーは使えなくなります。既存アプリを更新する際は、利用側のSecret更新と合わせて実施してください。

発行結果は `.private/apps/<アプリID>.json` に保存されます。`apiKey` を利用側WorkerのSecret（例: `LLM_JOBS_API_KEY`）に登録してください。実行者専用の `JOB_RUNNER_KEY` は利用側アプリへ渡しません。

### 7. アプリから登録し、結果を反映する

アプリのバックエンドから呼ぶ例です。キーをブラウザに渡さないでください。

```js
const base = 'https://hermes-llm-jobs.kazumasa.workers.dev';
const headers = {
  Authorization: `Bearer ${env.LLM_JOBS_API_KEY}`,
  'Content-Type': 'application/json',
};
const response = await fetch(`${base}/api/jobs`, {
  method: 'POST',
  headers,
  body: JSON.stringify({
    type: 'document.translate',
    version: 1,
    idempotencyKey: 'translate-document-123-en-revision-1',
    payload: { text: 'こんにちは。', targetLanguage: 'en' },
  }),
});
if (!response.ok) throw new Error(`ジョブ登録失敗: ${response.status}`);
const job = await response.json();
// job.id と自分のアプリの文書IDの対応を、自分のDBに保存する
```

別のリクエストや定期処理で結果を取得します。同じHTTPリクエスト内でLLMの完了を待ち続ける必要はありません。

```js
const response = await fetch(`${base}/api/jobs/${jobId}`, { headers });
if (!response.ok) throw new Error(`結果取得失敗: ${response.status}`);
const job = await response.json();
if (job.status === 'completed') {
  const translatedText = job.result.translation;
  // 自分のDBや画面に反映する。同じjob.idを再取得しても二重処理しないようにする
} else if (job.status === 'failed') {
  // 失敗として表示し、必要なら運用担当者へ知らせる
}
// pending / running の間は、後で再取得する
```

新しい種類を追加した後の動作確認では、短い文章を1件登録し、`completed` と期待する結果形式を確認します。実際にHermesを呼ぶため、サブスクの利用枠を消費します。日本時間8:00〜23:59にPCとHermesが起動していれば、通常は次の3分間隔の取得後に処理されます。混雑や再試行によって待ち時間は延びます。

| 症状 | 確認する場所 |
|---|---|
| 登録が401 | 利用側のアプリ用キー、キー再発行後のSecret更新 |
| 登録が400 | Workerの対応種類、アプリの許可種類、version、入力形式 |
| 登録が409 | 同じ重複防止キーで入力を変えていないか |
| pendingのまま | 時間帯、PCとHermes、cronの停止状態、runnerの `TYPES` |
| runningから再試行・failedになる | `hermes cron runs`、モデルの応答時間、両側の結果検証 |
| 別アプリから結果を取得すると404 | 登録したアプリと同じキーを使っているか |

## 独立性とクイズとの連携

このリポジトリはクイズのコードをimportせず、クイズのDBへアクセスしません。
クイズは通常のAPIクライアントです。Cloudflare上では別WorkerへのService Bindingで同じHTTP APIを呼び、アプリ用キーでも認証します。他アプリは公開HTTPS APIでも利用できます。

クイズ側の `quiz_job_outbox` に回答と送信待ちを同時保存し、障害後も重複防止キー付きで再送します。完了結果はクイズWorkerが取得し、クイズの点数・講評へ反映します。結果取得はクイズ側の5分ごとのCronと診断画面へのアクセス時に行います。共通サービスからクイズDBを直接更新する処理やWebhookはありません。

## デプロイ

```bash
npm ci
npm run db:remote
npx wrangler secret put JOB_RUNNER_KEY
npm run deploy
```

`wrangler.jsonc` はこの環境のアカウント・専用DBを指定しています。別環境ではWorker名・アカウント・DBとrunnerの許可URLを変更してください。

- `JOB_RUNNER_KEY`: 実行プログラム用の強いランダムキー。
- `llm_apps`: アプリID、キーのSHA-256、許可type、enabledを保存。生キーはDBに保存しません。
- `/health`: サービス識別用の公開ヘルス応答。
- 元のクイズURLの `/api/jobs` は廃止。認証情報を別URLへ転送するリダイレクトは行いません。

## PC側の実行（SQLite dispatcher）

Python標準ライブラリとLinuxの `/usr/bin/timeout` を使用します。追加のPythonパッケージは不要です。このPCのuutils coreutils 0.8.0でもwatchdog動作をテストしています。モデル環境（Hermes、Codexログイン、Claude Codeログイン）は従来と同じです。

`.private/config.json` を所有者限定権限で作成します（既存ファイルをそのまま使用）。キーをコマンドラインやログへ出しません。

```json
{"url":"https://hermes-llm-jobs.kazumasa.workers.dev","runnerKey":"Worker Secretと同じ値"}
```

```bash
cd /home/kazu/work/hermes-llm-jobs
python3 dispatcher.py tick
python3 dispatcher.py status
# 通常はtickが起動。手動の再開・診断時にも同じロックを使用する
python3 dispatcher.py worker --lane illustration
python3 dispatcher.py worker --lane common
python3 dispatcher.py worker --lane video
```

全サブコマンドに `--state-dir /absolute/private/directory` と `--config /absolute/config.json` を指定できます。既定は `.private/dispatcher` と `.private/config.json`。同一PCの全tick/workerで**同じstate-dir**を使用してください。`status` はAPIや設定ファイルを読みません。`--allow-loopback` は明示的なテスト用設定の `http://127.0.0.1:<port>` のみ許可するオプションで、本番では不要です。リダイレクトには従いません。

### 取得と3本の独立worker

- `tick` は非待機のグローバルflockで重複を避け、保存済みoutboxを最大2件再送し、既存の未完了処理を再開します。新規 `/claim` は**1回のtickにつき最大1回・1件**。commonが空なら `quiz.grade` / `document.summarize`、illustrationが空なら `illustration.svg`、videoが空なら `video.generate` を同じclaimの候補に入れます。全レーン使用中ならclaimしません。動画は10分以上かかりますが、専用レーンなので採点とイラストは待たされません。
- **新規取得のみ日本時間8:00〜23:59**。深夜でも既存処理の再開・結果再送は続けます。HTTP各呼び出しにはDNS・応答本文も含む5秒の制限を設けています。1tickのAPI待ち時間は最大3呼び出し分で、モデル処理はしません。内部タイマー/常駐ポーリングはありません。
- claimをSQLiteへcommitしてから `start_new_session=True` でworkerを起動します。stdinは `/dev/null`、stdout/stderrはレーン別のprivateログです。tickの正常終了後もworkerが動きます。スケジューラーによる明示的な取消・プロセスツリー強制終了まで防ぐ仕組みではありません。
- 各workerはレーン別flockを全処理中保持し、1回に1件処理して終了します。commonは既存 `runner.run_model`（`gpt-6-luna / Hermes / openai-codex`、`hermes-jobs-v1`）、illustrationは同関数から既存 `runner.draw_illustration`（`claude-opus-5-5 / Claude Code`、`illustration-v3`）、videoは `runner.make_video`（`claude-opus-5-5 / VOICEVOX / HyperFrames`、`auto-movie-v1`）。プロンプト・検証・モデル設定は変更していません。
- leaseTokenは不透明な文字列としてそのまま保存・送信します。期限切れの未処理claimはモデルを呼ばず `lease_lost` にします。モデル待機時間は既存上限（common150秒、illustration600秒、video2,400秒）と残りlease時間の小さい方に制限します。
- モデルCLIと独立watchdogへレーンのロックFDを継承します。workerがSIGKILLされても生き残るCLIがロックを持つ間は同レーンで再実行しません。watchdogの `/usr/bin/timeout --signal=KILL` が残り時間内にモデルのプロセスグループを終了させ、永久にロックが残ることを避けます。標準のモデルCLIを前提とし、別セッションへ自ら逃げる任意コマンドを扱う設計ではありません。

### 永続化・再送・復旧

- `.private/dispatcher/jobs.sqlite3` はWAL + `synchronous=FULL`。state-dirは0700、DB/ロック/ログは0600。入力・lease・結果を含む機密DBなのでGitへ追加しないでください。バックアップはworker停止後かSQLite backup APIを使用し、稼働中のDB本体だけをコピーしないでください。
- モデル結果または失敗理由とoutboxを**同じSQLiteトランザクション**で保存してからAPIへ送信します。tickとworkerの送信は別flockで直列化します。通信失敗・HTTP401/429/5xxなどはpendingのまま、次tickで同じ内容を再送します。認証不良は自動修復できないため `status` の `delivery_error` を確認してください。
- 完了POSTは既存APIの同一結果再送に対応し、remote成功後/local commit前に落ちても再送で回復します。**outboxの完了結果はlease期限後も送信**します（既にremoteで完了していた場合のack回収）。HTTP409は `lease_lost` として終端化し、記録した結果は残します。
- `/fail` はremote側でleaseを消すため、成功直後にackを失った再送は409になり得ます。この場合も `lease_lost` として保存し、成功と断定しません。APIを変更せずexactly-onceを保証することはできません。
- 完了POSTがHTTP400（例: UTF-16文字数によるbroker検証拒否）またはHTTP413（リクエスト本文の32KiB超過）の場合は `rejected` として結果を保存し、永久再送でレーンを塞ぎません。remoteのlease期限後、通常のclaim/最大3回の試行制限で再処理されます。結果を黙って修正・削除しません。
- workerロックが消えた後の `running` は、曖昧なモデル処理をローカルで再実行せず、leaseが有効ならdurableな `fail / runner_error` を送ります。remoteの5分後の再試行と最大3回の制限に委ねます。期限切れなら `lease_lost`。同じremote IDを新leaseで取得した場合も**別attempt**として履歴を残します。
- claimのremote成功からlocal commitまでのクラッシュ/応答喪失は、現行APIでは完全には閉じられません。その仕事はremoteのlease期限後に再取得可能です。モデル実行のexactly-onceは保証しません。
- `status` はJSONでattempt・ジョブID・type・状態・実行回数・期限・送信回数/エラーを表示し、入力・結果・leaseToken・認証キーは表示しません。履歴は自動削除しません。ログは `common.log` / `illustration.log` / `video.log`。正常な空tickは無出力でLLMを呼びません。

`hermes-call.py` はローカルHermesの内部Python APIを使います。空のツールセット、ユーザー設定・ルールを無効にした実行です。Hermes更新で内部APIが変われば対応が必要です。Hermes自身の会話保存方針は適用されます。

### 旧runnerからの切替

1. 旧common・illustration cronを停止し、起動済みrunnerの終了（旧ロックが空くこと）を確認します。旧runnerとdispatcherを同時運用しないでください（ロックは互換ではありません）。
2. `.private/result-*.json` と `.private/illustration-result-*.json` をバックアップし、旧runnerの再送で解消してから切り替えます。**新dispatcherは旧JSONを自動import/削除しません**。期限切れ・409の未送信結果も必要なら監査用に保管します。ファイルが残る状態で黙って切り替えないでください。
3. 下記の単一cronへ切り替えます。新設定の検証後、旧illustration cron `507625c90cee` を削除します。`runner.py` と旧ラッパーは変更せず残しています。ロールバックも先に新cronを停止し、worker終了と新outboxの解消を確認してから行います。

### オフラインテスト

```bash
python3 -m unittest test_dispatcher -v
```

全テストは一時ディレクトリのSQLite、ループバックHTTPスタブ、偽モデルを使用します。本番API・本番モデル・実際の `.private` DBを使いません。長いillustrationと短いcommonの並行動作、親tick終了、重複tick、空キュー、lease期限、再送/409/400/413、ack直後のクラッシュ、worker死亡後に残るCLI/独立watchdogを検証します。

## このPCの定期設定（切替後）

- Hermes cron ID: `6df0ad98fae9` / 「LLMジョブ取得・振り分け」
- cron式: `*/3 * * * *`（3分間隔、新規取得の時間帯はdispatcher側で日本時間に制限）
- スクリプト: `~/.hermes/scripts/llm-job-dispatcher.py` → `dispatcher.py tick`
- `no_agent=true`、`deliver=local`
- workdir / リポジトリ: `/home/kazu/work/hermes-llm-jobs`
- 旧illustration cron `507625c90cee` は切替検証後に削除。Codexアプリの旧定期採点 `ai` は停止済み

外側のスケジューラーはLLMを呼びません。cronの成功は「取得・振り分けが終了した」意味で、非同期workerの完了を意味しません。`hermes cron runs` と `python3 dispatcher.py status` の両方で確認します。コードの導入だけではcron設定を自動変更しません。

## 移行

旧クイズDBのジョブ・アプリ認証は専用DBへコピーし、元テーブルはロールバック用の保管として残しています。現在のクイズの実行コードからは利用しません。秘密鍵、移行スナップショット、処理結果キャッシュは `.private/` 配下でGit対象外です。
