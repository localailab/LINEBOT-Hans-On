## 📁 Repository Structure

| Path | Purpose |
| --- | --- |
| `lambda_function.py` | AWS Lambda のエントリポイント (`handler(event, context)`) |
| `character.py`      | キャラクター設定や共通ロジックを定義 |
| `linebot-layer.zip` | LINE Messaging API SDK などをまとめた Lambda Layer |
| `openai-layer.zip`  | OpenAI SDK (`openai`, `tiktoken`, ほか) をまとめた Lambda Layer |

---

## 🔧 Environment Variables

| Name | Issued by | Description |
| --- | --- | --- |
| `CHANNEL_ACCESS_TOKEN` | **LINE Developers** | Bot がメッセージ送信に使うアクセストークン |
| `CHANNEL_SECRET` | **LINE Developers** | Webhook 署名検証用シークレット |
| `OPENAI_API_KEY` | **OpenAI** | GPT 呼び出し用 API キー |