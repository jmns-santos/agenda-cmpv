# Agenda municipal em .ics (agenda.ics sempre atualizada)

## O que faz o script

`gerar_agenda_ics.py` le os eventos futuros diretamente do endpoint JSON
que o site do cm-pvarzim.pt ja expoe internamente
(`https://www.cm-pvarzim.pt/wp-json/plone/v1/event`) e escreve um
ficheiro `agenda.ics` (formato iCalendar, RFC 5545) com todos os
eventos a partir de hoje.

Nao faz scraping de HTML - usa o mesmo JSON que alimenta a pagina
`/eventos/` do site, por isso e' mais estavel do que ler a pagina web
(nao depende do layout, e traz titulo, datas, local, descricao e link
de cada evento ja estruturados).

Cada vez que corre, sobrescreve o `agenda.ics` de forma atomica (escreve
para um ficheiro temporario e so troca no fim), para nunca deixar o
ficheiro publicado a meio de uma escrita.

## Testar localmente

```
pip install -r requirements.txt
python gerar_agenda_ics.py
```

Isto cria `agenda.ics` na mesma pasta do script e um `gerar_agenda_ics.log`
com o registo da execucao (quantos eventos encontrou, erros, etc).
Abre o `agenda.ics` num leitor de calendario (Outlook, Calendario do
telemovel) para confirmar que os eventos e horas estao corretos.

Parametros uteis:

```
python gerar_agenda_ics.py --output C:\caminho\agenda.ics --horizonte-meses 12 --ttl-horas 6
```

## Agendar na VM (Windows Task Scheduler)

Tal como o `Waze_to_Sigdb.py` / `resopark_update.py`: cria uma tarefa
agendada a correr `python gerar_agenda_ics.py` a apontar para uma pasta
fixa (ex. `C:\Scripts\Agenda\agenda.ics`). A agenda municipal nao muda
de hora a hora, por isso correr **a cada 3-6h** e' mais que suficiente
(usa o mesmo valor em `--ttl-horas`, e' a "dica" que fica gravada no
.ics a dizer aos calendarios de quanto em quanto tempo devem verificar
se ha novidades).

## Publicar o link (a parte que falta para chegar ao telemovel)

O script so gera o ficheiro na VM. Para as pessoas conseguirem
descarregar/subscrever a partir do site, esse `agenda.ics` tem de ficar
acessivel por HTTPS a partir de `cm-pvarzim.pt` (ou de um subdominio
publico). Isso e' uma decisao de alojamento, nao do script - duas
formas de o resolver:

**Opcao A - copiar para dentro do site WordPress (mais simples)**
No fim do `gerar_agenda_ics.py`, acrescentar um passo que envia o
`agenda.ics` por SFTP/FTP (ou copia de rede, se houver acesso SMB ao
servidor do site) para uma pasta publica do WordPress, por exemplo
`wp-content/uploads/agenda/agenda.ics`. Ficaria acessivel em
`https://www.cm-pvarzim.pt/wp-content/uploads/agenda/agenda.ics`.
Depois e' so criar um link normal numa pagina do site. Para isto
precisas de credenciais de escrita nessa pasta - e' a equipa que gere o
alojamento do site (ou quem trata do WordPress) que te pode dar isso.
Posso acrescentar o passo de upload ao script assim que souberes qual
o metodo de acesso (SFTP e' o mais simples de automatizar).

**Opcao B - expor a partir da propria VM**
Como a VM ja corre servicos internos (ex. sig.cm-pvarzim.pt), pedir aos
sistemas/rede para publicar essa pasta num subdominio (ex.
`agenda.cm-pvarzim.pt/agenda.ics`) via IIS + regra de DMZ/firewall.
Fica independente do WordPress, mas precisa de DNS e rede novos - mais
trabalho de coordenacao.

Recomendo a Opcao A para comecar, por ser mais rapida de pores no ar.

## Download vs. subscricao - importante para o objetivo final

Ha uma diferenca que afeta diretamente o "manter a agenda atualizada no
telemovel":

- Um link normal (`https://www.cm-pvarzim.pt/.../agenda.ics`) que as
  pessoas *descarregam e importam* no calendario cria uma copia
  estatica - nao se atualiza sozinha depois.
- Para o calendario do telemovel **acompanhar sempre a versao atual**,
  as pessoas tem de **subscrever** esse URL como calendario (nao
  simplesmente descarregar o ficheiro):
  - iPhone: Definicoes > Calendario > Contas > Adicionar Conta >
    Outra > Adicionar Calendario Subscrito, colar o link.
  - Android/Google Calendar: no Google Calendar (web) > Outros
    calendarios > Adicionar por URL, colar o link.
  - Um link com o esquema `webcal://` (em vez de `https://`) abre esse
    fluxo de subscricao automaticamente em muitos telemoveis/Outlook,
    em vez de descarregar o ficheiro - vale a pena disponibilizar as
    duas versoes do link na pagina do site (`https://...agenda.ics` e
    `webcal://www.cm-pvarzim.pt/.../agenda.ics`).

Vale a pena escrever isto na propria pagina do site ("para manter a
agenda sempre atualizada no teu telemovel, subscreve este link em vez
de o descarregar"), porque a maioria das pessoas vai simplesmente
clicar e descarregar se nao houver essa indicacao.

## Opcao C - GitHub Pages (gratuito, no ar hoje, sem depender de sistemas/IT)

Esta pasta ja esta preparada para isto: tem `.github/workflows/atualizar-agenda.yml`
e `index.html` prontos.

**O que faz:** em vez de correres o script na VM, o proprio GitHub corre-o
automaticamente a cada 4h (GitHub Actions - gratuito e sem limite para
repositorios publicos), gera o `agenda.ics` e publica-o. Nao depende da
VM estar ligada nem do Task Scheduler.

**Passos (uma vez so):**

1. Cria uma conta GitHub, se ainda nao tiveres (gratuita, github.com).
2. Cria um repositorio **publico** novo, ex. `agenda-cmpv`.
3. Envia o conteudo desta pasta inteira para esse repositorio (via
   GitHub Desktop, VS Code, ou linha de comandos):
   ```
   git init
   git add .
   git commit -m "Agenda municipal em .ics"
   git branch -M main
   git remote add origin https://github.com/<o-teu-utilizador>/agenda-cmpv.git
   git push -u origin main
   ```
4. No repositorio, vai a **Settings > Pages** e em "Build and deployment"
   escolhe **Deploy from a branch**, branch `main`, pasta `/ (root)`. Grava.
5. No fim de alguns minutos o site fica disponivel em
   `https://<o-teu-utilizador>.github.io/agenda-cmpv/`
6. Confirma em **Actions** que o workflow "Atualizar agenda.ics" correu
   sem erros (corre logo a seguir ao push, e depois sozinho a cada 4h).

O link do `.ics` fica em
`https://<o-teu-utilizador>.github.io/agenda-cmpv/agenda.ics`, e a pagina
`index.html` ja tem os dois links (subscrever / descarregar) prontos -
o link de subscricao ajusta-se sozinho ao dominio final, nao precisas de
editar nada no HTML.

**Nota honesta:** isto fica fora do dominio oficial `cm-pvarzim.pt`
(fica em `github.io`). E' a forma mais rapida e 100% gratuita de teres
algo a funcionar ja, mas para uma solucao "oficial" a Opcao A ou B
(publicar dentro do proprio dominio da camara) continua a ser preferivel
a prazo - podes sempre, mais tarde, apontar um subdominio proprio (ex.
`agenda.cm-pvarzim.pt`) para este GitHub Pages via DNS (CNAME), o que so
precisa de um pedido simples aos sistemas (um registo DNS), sem abrir
portas nem mexer no WordPress.

**Sobre o agendamento:** o GitHub desativa workflows agendados ao fim de
60 dias sem atividade no repositorio. Como este workflow faz sempre um
commit em cada corrida (o DTSTAMP do calendario muda sempre), isso nunca
deve acontecer aqui - e' so uma nota para o caso de veres o agendamento
parado sem explicacao.
