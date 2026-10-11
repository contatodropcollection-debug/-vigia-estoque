#!/usr/bin/env python3
"""
Vigia de Estoque - Jean (Pokémon TCG)
Confere o estoque dos fornecedores e avisa no Telegram quando algo volta / aparece.

Sites:
  - Super TCG  -> consulta pública de estoque (sem login)
  - SPKids     -> consulta pública de estoque (sem login, só disponibilidade)
  - Reval      -> página de busca, usando a sessão do seu Chrome
  - TCG Pro    -> catálogo, usando a sessão do seu Chrome (o vigia NÃO faz login)
  (Copag B2B fica de fora: os termos de uso proíbem robôs para monitorar estoque.)

Senhas/token NUNCA ficam neste arquivo: o token do Telegram fica no
"Acesso às Chaves" (Keychain) do Mac.
"""
import json, random, re, subprocess, sys, time, glob, os, warnings
warnings.filterwarnings("ignore")
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PASTA = Path(__file__).resolve().parent
ESTADO = PASTA / "estado.json"
OFFSET = PASTA / "telegram_offset.json"
SESSAO = PASTA / "sessao.json"   # cópia da sua sessão (Reval/TCG Pro), só no seu Mac
LOG = PASTA / "vigia.log"

# ----------------- CONFIGURAÇÃO (pode editar) -----------------
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8335108338")   # seu Id (não é segredo)
KEYCHAIN_SERVICO = "vigia-estoque"         # onde o token fica guardado
AVISAR_PRODUTO_NOVO_ESGOTADO = True        # avisar também quando aparece produto novo esgotado
PAUSA_ENTRE_PAGINAS = (2, 5)
NUVEM = bool(os.environ.get("GITHUB_ACTIONS"))  # rodando no GitHub?               # segundos (aleatório) entre páginas do mesmo site

# Filtro para a SPKids (vende outras coisas além de Pokémon)
# ---- FILTRO DE INTERESSE: só produtos selados de cartas Pokémon ----
# Cartas avulsas (ex.: "Goldeen (020/131)", "Lunala-GX (PR-SM_SM103A)")
AVULSA = r"\b\d{1,3}\s*/\s*\d{2,3}\b|\((PR-|SWSH|SVP|SV\d|SM\d|TG\d|GG\d|XY\d|BW\d)[^)]*\)"
# Precisa ter uma destas palavras (tipo de produto selado)
SELADO = (r"booster|display|blister|\bbox\b|caixa|cole[cç][aã]o|treinador avan|\betb\b|elite trainer|deck|baralho|"
          r"\btin\b|lata|bundle|combo|\bkit\b|pacote|\bpack\b|desafio estrat|ferramentas de treinador|"
          r"jogo de cartas|premium collection")
# Acessórios e outros itens que nunca interessam
NUNCA = (r"expositora|acr[ií]lico|sleeve|shield|deck ?box|top ?loader|playmat|tapete|mochila|estojo|"
         r"pel[uú]cia|chaveiro|camiseta|caneca|garrafa|\bdados?\b|servi[cç]o de avalia")
# Só entram se vierem junto com box/coleção/booster/blister (ex.: "Box Coleção Fichário" sim, "Fichário 2x2" não)
SO_SE_BOX = r"miniatura|figur|fich[aá]rio|binder|portf[oó]lio|pasta|boneco"
FORTE = r"\bbox\b|cole[cç][aã]o|booster|blister"


def interessa(nome):
    if re.search(AVULSA, nome, re.I) or re.search(NUNCA, nome, re.I):
        return False
    if not re.search(SELADO, nome, re.I):
        return False
    if re.search(SO_SE_BOX, nome, re.I) and not re.search(FORTE, nome, re.I):
        return False
    return True


FILTRO_SPKIDS = r"pok[eé]mon|\bME0?\d|celebra|escurid|caos|equil[ií]brio|her[oó]is|treinador|blister|booster|\betb\b|escarlate|mega evolu"
# ---------------------------------------------------------------

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 VigiaEstoquePessoal/1.0"
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})


class SessaoExpirada(Exception):
    pass


def log(msg):
    linha = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(linha)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(linha + "\n")
    # mantém o log pequeno
    try:
        if LOG.stat().st_size > 500_000:
            linhas = LOG.read_text(encoding="utf-8").splitlines()[-2000:]
            LOG.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    except Exception:
        pass


def pausa():
    time.sleep(random.uniform(*PAUSA_ENTRE_PAGINAS))


def preco_br(v):
    try:
        return f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return ""


# ----------------- Telegram -----------------
def token_telegram():
    if os.environ.get("TELEGRAM_TOKEN"):
        return os.environ["TELEGRAM_TOKEN"].strip()
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICO, "-a", "telegram", "-w"],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("Token do Telegram não encontrado no Acesso às Chaves. Rode: bash instalar.sh")
    return r.stdout.strip()


def telegram(texto):
    tk = token_telegram()
    ok = True
    for parte in [texto[i:i + 3900] for i in range(0, len(texto), 3900)]:
        r = requests.post(f"https://api.telegram.org/bot{tk}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": parte,
                                "parse_mode": "HTML", "disable_web_page_preview": "true"},
                          timeout=20)
        if not r.ok:
            ok = False
            log(f"ERRO Telegram: {r.status_code} {r.text[:200]}")
            if r.status_code in (401, 404):
                log("-> O token parece errado. Guarde de novo (veja o passo do token).")
    return ok


def esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ----------------- Sessão (login) da Reval e TCG Pro -----------------
# Rodando pelo Terminal, o vigia copia a sua sessão do Chrome para "sessao.json".
# Rodando sozinho (em segundo plano), o macOS não deixa ler o Chrome, então ele usa essa cópia
# e vai atualizando-a sozinho. Se a sessão expirar: entre no site pelo Chrome e rode no Terminal
#   ~/vigia-estoque/.venv/bin/python ~/vigia-estoque/vigia.py --renovar
PODE_LER_CHROME = not NUVEM and (sys.stdout.isatty() or "--renovar" in sys.argv)


def ler_sessoes():
    try:
        return json.loads(SESSAO.read_text(encoding="utf-8"))
    except Exception:
        return {}


def gravar_sessao(dominio, cookies):
    d = ler_sessoes()
    d[dominio] = cookies
    SESSAO.write_text(json.dumps(d), encoding="utf-8")
    os.chmod(SESSAO, 0o600)  # só o seu usuário do Mac consegue ler


def cookies_chrome(url):
    """Pega a sessão já aberta no seu Chrome (procura em todos os perfis)."""
    from pycookiecheat import chrome_cookies
    base = Path.home() / "Library/Application Support/Google/Chrome"
    arquivos = sorted(glob.glob(str(base / "*/Cookies")), key=lambda p: ("Default" not in p, p))
    for arq in arquivos:
        try:
            c = chrome_cookies(url, cookie_file=arq)
            if c:
                return c
        except Exception as e:
            log(f"aviso cookies {arq}: {e}")
    return {}


def sessao_logada(dominio):
    """Cria uma conexão só para este site, com a sua sessão."""
    s = requests.Session()
    s.headers.update(S.headers)
    cookies = cookies_chrome(f"https://{dominio}/") if PODE_LER_CHROME else {}
    if cookies:
        gravar_sessao(dominio, cookies)
    else:
        cookies = ler_sessoes().get(dominio, {})
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=dominio)
    return s


def guardar_sessao_atualizada(dominio, s):
    """O site às vezes renova a sessão; guardamos a versão mais nova."""
    try:
        gravar_sessao(dominio, {c.name: c.value for c in s.cookies})
    except Exception as e:
        log(f"aviso ao guardar sessão {dominio}: {e}")


# ----------------- Sites -----------------
def woo_store(base, filtro=None, excluir=None, com_preco=True):
    """Lojas WooCommerce: consulta pública /wp-json/wc/store/v1/products."""
    itens, pagina = {}, 1
    while True:
        r = S.get(f"{base}/wp-json/wc/store/v1/products",
                  params={"per_page": 100, "page": pagina}, timeout=30)
        r.raise_for_status()
        dados = r.json()
        for p in dados:
            nome = BeautifulSoup(p.get("name", ""), "html.parser").get_text()
            if filtro and not re.search(filtro, nome, re.I):
                continue
            if excluir and re.search(excluir, nome, re.I):
                continue
            preco = ""
            if com_preco and p.get("prices", {}).get("price"):
                pr = p["prices"]
                preco = preco_br(int(pr["price"]) / (10 ** int(pr.get("currency_minor_unit", 2))))
            itens[str(p["id"])] = {"nome": nome, "disp": bool(p.get("is_in_stock")),
                                   "url": p.get("permalink", ""), "preco": preco}
        total_pag = int(r.headers.get("X-WP-TotalPages", "1") or 1)
        if pagina >= total_pag or not dados:
            break
        pagina += 1
        pausa()
    return itens


def super_tcg():
    return woo_store("https://supertcg.com.br")


def spkids():
    # Preço não é usado: a SPKids mostra preço só para lojista logado.
    return woo_store("https://spkidsdistribuidora.com.br", filtro=FILTRO_SPKIDS, com_preco=False)


def reval_login():
    """Login automático na Reval (usado na nuvem). Usuário e senha vêm dos Secrets do GitHub."""
    s = requests.Session()
    s.headers.update(S.headers)
    r = s.get("https://www.reval.net/login", timeout=30)
    r.raise_for_status()
    form = BeautifulSoup(r.text, "html.parser").find("form")
    dados = {i["name"]: i.get("value", "") for i in form.find_all("input")
             if i.get("name") and i.get("type", "text") not in ("submit", "button", "image", "checkbox")}
    dados["ctl00$MainContent$wucLoginCliente$txtUsuario"] = os.environ["REVAL_USUARIO"]
    dados["ctl00$MainContent$wucLoginCliente$txtSenha"] = os.environ["REVAL_SENHA"]
    dados["ctl00$MainContent$wucLoginCliente$btnEntrar"] = "Entrar"
    r2 = s.post("https://www.reval.net/login", data=dados, timeout=30,
                headers={"Referer": "https://www.reval.net/login", "Origin": "https://www.reval.net"})
    # Diagnóstico (sem mostrar usuário/senha)
    t = r2.text.lower()
    sinais = [k for k in ("sair", "olá", "inválid", "incorret", "não encontrado", "bloque", "captcha",
                          "cnpj", "e-mail", "aguarde", "faça login") if k in t]
    titulo = BeautifulSoup(r2.text, "html.parser").title
    log(f"Reval login: status={r2.status_code} pagina={r2.url.split('?')[0]} tam={len(r2.text)} "
        f"titulo={(titulo.get_text(strip=True) if titulo else '')[:40]!r} sinais={sinais} "
        f"campos={len(dados)} usuario_tam={len(os.environ['REVAL_USUARIO'])} cookies={sorted(c.name for c in s.cookies)}")
    msgs = [x.get_text(' ', strip=True)[:120] for x in BeautifulSoup(r2.text, 'html.parser').select(
        '.alert, .error, .erro, .validation-summary-errors, [id*=lblMsg], [id*=lblErro], [id*=Mensagem], .msg')]
    if msgs:
        log(f"Reval login mensagens: {msgs[:3]}")
    return s


def reval():
    if os.environ.get("REVAL_USUARIO"):
        S = reval_login()
    else:
        S = sessao_logada("www.reval.net")
    itens, pagina = {}, 1
    while pagina <= 10:
        r = S.get("https://www.reval.net/pesquisa/result", params={"q": "pokemon", "p": pagina}, timeout=30)
        r.raise_for_status()
        sopa = BeautifulSoup(r.text, "html.parser")
        cards = sopa.select("ul.products-grid li.item")
        if not cards:
            break
        if pagina == 1 and "Faça login para visualizar o preço" in r.text:
            raise SessaoExpirada("Reval")
        novos = 0
        for li in cards:
            cod = li.select_one(".product-cod")
            a = li.select_one('a[href*="/produto/"]')
            if not cod or not a:
                continue
            codigo = re.sub(r"\D", "", cod.get_text())
            texto = li.get_text(" ", strip=True)
            na = li.select_one(".product-name a")
            nome = (na.get("title") or na.get_text(" ", strip=True)) if na else f"Código {codigo}"
            estoque = (li.select_one(".div-stock") or li).get_text(" ", strip=True)
            m = re.search(r"Estoque:\s*(.{1,40}?)(?:\s+Novidade|\s*$)", estoque)
            disp = bool(li.select_one(".bt-add-cart")) and not re.search(r"esgotad|indispon|avise", texto, re.I)
            pr = re.search(r"R\$\s*[\d.]+,\d{2}", texto)
            if codigo not in itens:
                novos += 1
            itens[codigo] = {"nome": nome, "disp": disp,
                             "url": "https://www.reval.net" + a["href"] if a["href"].startswith("/") else a["href"],
                             "preco": pr.group(0) if pr else "",
                             "extra": ("Estoque: " + m.group(1)) if m else ""}
        if novos == 0:
            break
        pagina += 1
        pausa()
    if not os.environ.get("REVAL_USUARIO"):
        guardar_sessao_atualizada("www.reval.net", S)
    return itens


def tcg_pro():
    S = sessao_logada("tcgpro.com.br")
    r = S.get("https://tcgpro.com.br/catalogo", timeout=30)
    r.raise_for_status()
    if "Faça login para ver o preço" in r.text:
        raise SessaoExpirada("TCG Pro")
    sopa = BeautifulSoup(r.text, "html.parser")
    itens = {}
    for art in sopa.select("article"):
        a = art.select_one('a[href*="/produto"]')
        h = art.select_one("h3, h2")
        if not a or not h:
            continue
        link = a["href"].split("?")[0]
        texto = art.get_text(" ", strip=True)
        precos = re.findall(r"R\$\s*[\d.]+,\d{2}", texto)
        itens[link] = {"nome": h.get_text(" ", strip=True), "disp": "esgotado" not in texto.lower(),
                       "url": "https://tcgpro.com.br" + link if link.startswith("/") else link,
                       "preco": precos[0] if precos else ""}
    guardar_sessao_atualizada("tcgpro.com.br", S)
    return itens


SITES = {
    "Super TCG": super_tcg,
    "SPKids": spkids,
    "Reval": reval,
    "TCG Pro": tcg_pro,
}
# Escolher quais sites este computador vigia (ex.: VIGIA_SITES="supertcg,spkids,reval")
_escolha = os.environ.get("VIGIA_SITES", "")
if not _escolha and (PASTA / "sites.txt").exists():
    _escolha = (PASTA / "sites.txt").read_text()
if _escolha.strip():
    _quero = {re.sub(r"[^a-z]", "", x.lower()) for x in _escolha.split(",")}
    SITES = {k: v for k, v in SITES.items() if re.sub(r"[^a-z]", "", k.lower()) in _quero}


# ----------------- Lógica principal -----------------
def carregar():
    if ESTADO.exists():
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    return {"sites": {}, "falhas": {}, "avisos": {}}


def salvar(est):
    ESTADO.write_text(json.dumps(est, ensure_ascii=False, indent=1), encoding="utf-8")


def linha(it):
    partes = [f"• <b>{esc(it['nome'])}</b>"]
    if it.get("preco"):
        partes.append(esc(it["preco"]))
    if it.get("extra"):
        partes.append(esc(it["extra"]))
    return " — ".join(partes) + f"\n  {it['url']}"


def rodar(teste=False):
    est = carregar()
    primeira_vez = not est["sites"]
    mensagens, resumo = [], []

    for nome_site, func in SITES.items():
        try:
            atual = {k: v for k, v in func().items() if interessa(v["nome"])}
            if not atual:
                raise RuntimeError("nenhum produto lido (a página pode ter mudado)")
        except SessaoExpirada:
            log(f"{nome_site}: sessão expirada")
            if not est["avisos"].get(nome_site) and NUVEM:
                mensagens.append(f"🔑 <b>{nome_site}</b>: o login automático não funcionou. "
                                 f"Confira o usuário e a senha nos Secrets do GitHub.")
                est["avisos"][nome_site] = True
            elif not est["avisos"].get(nome_site):
                mensagens.append(f"🔑 <b>{nome_site}</b>: sua sessão expirou.\n"
                                 f"1) Abra o site no Chrome e entre na sua conta.\n"
                                 f"2) No Terminal do Mac, rode:\n"
                                 f"<code>~/vigia-estoque/.venv/bin/python ~/vigia-estoque/vigia.py --renovar</code>")
                est["avisos"][nome_site] = True
            continue
        except Exception as e:
            n = est["falhas"].get(nome_site, 0) + 1
            est["falhas"][nome_site] = n
            log(f"{nome_site}: ERRO ({n}x seguidas): {e}")
            if n == 3:
                mensagens.append(f"⚠️ <b>{nome_site}</b> não está sendo lido há 3 tentativas seguidas.\n"
                                 f"Erro: {esc(str(e))[:200]}")
            continue

        if est["falhas"].get(nome_site, 0) >= 3 or est["avisos"].get(nome_site):
            mensagens.append(f"✅ <b>{nome_site}</b> voltou a ser lido normalmente.")
        est["falhas"][nome_site] = 0
        est["avisos"][nome_site] = False

        antes = est["sites"].get(nome_site)
        voltou, novo_disp, novo_esg = [], [], []
        if antes is not None:
            for pid, it in atual.items():
                old = antes.get(pid)
                if old is None:
                    (novo_disp if it["disp"] else novo_esg).append(it)
                elif it["disp"] and not old["disp"]:
                    voltou.append(it)

        disp = sum(1 for i in atual.values() if i["disp"])
        resumo.append(f"• {nome_site}: {len(atual)} selados, {disp} disponíveis")
        log(f"{nome_site}: {len(atual)} produtos, {disp} disponíveis, "
            f"{len(voltou)} voltaram, {len(novo_disp)} novos disp., {len(novo_esg)} novos esgot.")

        if voltou:
            mensagens.append(f"🟢 <b>VOLTOU AO ESTOQUE — {nome_site}</b>\n" + "\n".join(linha(i) for i in voltou))
        if novo_disp:
            mensagens.append(f"🆕 <b>PRODUTO NOVO DISPONÍVEL — {nome_site}</b>\n" + "\n".join(linha(i) for i in novo_disp))
        if novo_esg and AVISAR_PRODUTO_NOVO_ESGOTADO:
            mensagens.append(f"👀 <b>Produto novo (ainda esgotado) — {nome_site}</b>\n" + "\n".join(linha(i) for i in novo_esg))

        est["sites"][nome_site] = atual
        if nome_site != list(SITES)[-1]:
            time.sleep(random.uniform(1, 3))

    salvar(est)

    if primeira_vez or teste:
        mensagens.insert(0, "🤖 <b>Vigia de estoque ligado!</b>\n" + "\n".join(resumo) +
                         "\n\nA partir de agora eu só aviso quando algo mudar.\n"
                         "Mande /disponiveis para ver a lista do que está disponível agora.")
    for m in mensagens:
        telegram(m)


# ----------------- Comandos pelo Telegram -----------------
AJUDA = ("Comandos:\n"
         "/disponiveis - lista dos selados disponíveis agora (todos os sites)\n"
         "/disponiveis reval - só de um site (supertcg, spkids, reval, tcgpro)\n"
         "/status - última leitura de cada site")


def lista_disponiveis(filtro=""):
    est = carregar()
    if not est["sites"]:
        return "Ainda não fiz a primeira leitura. Tente de novo em alguns minutos."
    f = re.sub(r"[^a-z]", "", filtro.lower())
    partes = []
    for site, itens in est["sites"].items():
        if f and f not in re.sub(r"[^a-z]", "", site.lower()):
            continue
        disp = sorted((i for i in itens.values() if i["disp"]), key=lambda i: i["nome"])
        if not disp:
            partes.append(f"<b>{site}</b>: nada disponível agora.")
            continue
        partes.append(f"<b>{site}</b> ({len(disp)} disponíveis)\n" + "\n".join(linha(i) for i in disp))
    if not partes:
        return "Não achei esse site. Use: supertcg, spkids, reval ou tcgpro."
    quando = datetime.fromtimestamp(ESTADO.stat().st_mtime).strftime("%d/%m %H:%M")
    return f"📦 <b>Disponíveis agora</b> (leitura de {quando})\n\n" + "\n\n".join(partes)


def status():
    est = carregar()
    quando = datetime.fromtimestamp(ESTADO.stat().st_mtime).strftime("%d/%m %H:%M") if ESTADO.exists() else "-"
    linhas = [f"🩺 <b>Status</b> (última leitura: {quando})"]
    for site in SITES:
        itens = est["sites"].get(site, {})
        prob = ("🔑 sessão expirada" if est["avisos"].get(site)
                else f"⚠️ {est['falhas'].get(site)} falhas seguidas" if est["falhas"].get(site) else "✅ ok")
        linhas.append(f"• {site}: {len(itens)} selados, {sum(1 for i in itens.values() if i['disp'])} disponíveis — {prob}")
    return "\n".join(linhas)


def comandos():
    """Responde comandos mandados ao robô. Roda a cada 1 minuto (leve: só fala com o Telegram)."""
    tk = token_telegram()
    off = json.loads(OFFSET.read_text()) if OFFSET.exists() else 0
    r = requests.get(f"https://api.telegram.org/bot{tk}/getUpdates",
                     params={"offset": off, "timeout": 0, "allowed_updates": '["message"]'}, timeout=20)
    if not r.ok:
        log(f"ERRO getUpdates: {r.status_code}")
        return
    for up in r.json().get("result", []):
        off = up["update_id"] + 1
        msg = up.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != TELEGRAM_CHAT_ID:
            continue  # ignora qualquer outra pessoa
        txt = (msg.get("text") or "").strip()
        cmd, _, arg = txt.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/disponiveis", "/disponíveis", "/lista"):
            telegram(lista_disponiveis(arg))
        elif cmd == "/status":
            telegram(status())
        else:
            telegram(AJUDA)
    OFFSET.write_text(json.dumps(off))


if __name__ == "__main__":
    if "--teste-telegram" in sys.argv:
        if telegram("✅ Teste: o robô consegue te mandar mensagem!"):
            print("Mensagem de teste enviada. Veja no Telegram.")
        else:
            print("ERRO: o Telegram recusou. O token provavelmente está errado.")
            sys.exit(1)
    elif "--comandos" in sys.argv:
        comandos()
    else:
        try:
            rodar(teste="--teste" in sys.argv or "--renovar" in sys.argv)
        finally:
            if NUVEM:
                try:
                    comandos()  # na nuvem, responde /disponiveis a cada rodada
                except Exception as e:
                    log(f"ERRO comandos: {e}")
