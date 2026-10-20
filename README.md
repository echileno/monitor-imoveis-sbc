# Monitor de aluguel — Domos, Trilage e Anima (São Bernardo do Campo)

Roda de hora em hora no GitHub Actions, procura anúncios de aluguel nesses 3 condomínios
em **OLX, ZAP Imóveis, Viva Real, Imovelweb e Chaves na Mão** e avisa no Telegram quando:

- aparece um anúncio **novo** com aluguel **até R$ 4.200** (só o aluguel; condomínio/IPTU não contam);
- um anúncio que você já tinha visto **baixa de preço**.

Anúncios já avisados ficam registrados em `state.json` (o próprio workflow salva), então você não
recebe o mesmo anúncio duas vezes.

## Configuração (uma vez só)

### 1. Criar o bot do Telegram
1. No Telegram, converse com **@BotFather** e envie `/newbot`. Guarde o **token** que ele devolver.
2. Abra uma conversa com o seu bot novo e mande qualquer mensagem (ex.: "oi").
3. No navegador, abra `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates` e procure
   `"chat":{"id": 123456789` — esse número é o seu **chat id**.

> Se você já criou um bot para o monitor da passagem do Chile, pode reutilizar o mesmo token e chat id.

### 2. Criar o repositório novo e subir os arquivos
Crie um repositório (pode ser privado) e envie esta pasta inteira, incluindo `.github/`.

### 3. Cadastrar os secrets
No repositório: **Settings → Secrets and variables → Actions → New repository secret**

| Nome | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token do BotFather |
| `TELEGRAM_CHAT_ID` | seu chat id |

### 4. Ligar
Aba **Actions → Monitor Imoveis SBC → Run workflow**. Na primeira execução você recebe
"✅ Monitor ligado" com o total de anúncios encontrados; depois disso, só novidades.

## Ajustes (arquivo `config.yaml`)

- `max_rent`: teto do aluguel.
- `condos`: nomes e apelidos aceitos no texto do anúncio.
- `sources`: URL de busca de cada site (`{q}` é trocado pelo nome do condomínio).
- `warn_after_empty_runs`: quantas execuções seguidas sem nenhum anúncio lido até avisar que uma fonte quebrou.

## Se uma fonte parar de funcionar

Sites de imóveis costumam bloquear robôs ou mudar o layout. Você será avisado no Telegram
(⚠️) quando uma fonte ficar vazia por várias execuções seguidas. Para investigar:

1. Abra a busca no navegador e confira se a URL do `config.yaml` ainda é parecida com a do site.
   Se mudou, copie a nova trocando o nome do condomínio por `{q}`.
2. Rode local com `python monitor.py --dry-run --debug`: ele salva o HTML baixado em `debug/`
   e mostra quantos anúncios leu de cada fonte. Se o HTML for uma página de "verificação/captcha",
   o site está bloqueando o robô (nesse caso as outras fontes continuam funcionando).

## Rodar local para testar

```bash
pip install -r requirements.txt
python monitor.py --dry-run          # não envia Telegram e não grava estado
TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python monitor.py
```

## Custo

Cada execução leva poucos minutos (15 buscas), então 24 execuções por dia ficam bem
abaixo da cota gratuita do GitHub Actions, especialmente em repositório público (ilimitado).

## Limitações honestas

- A extração é genérica (lê JSON embutido nas páginas e, se não houver, o HTML dos cards).
  Funciona sem depender de um layout específico, mas não é garantida: algum site pode exigir ajuste.
- Um anúncio só é aceito se o texto dele citar o condomínio (Domos, Trilage ou Anima).
  Anúncios que não mencionam o nome não são encontrados.
- O preço lido é o aluguel anunciado; confira sempre o valor no anúncio antes de contatar.
