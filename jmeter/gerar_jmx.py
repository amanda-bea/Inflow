"""Gera InflowMonolito.jmx e InflowMicrosservicos.jmx a partir de UM único fluxo.

Fluxo (label do transaction controller: "Fluxo completo"):
  1. POST sign-up (usuário novo, e-mail único)
  2. POST sign-in
  3. GET  account
  4. Aguarda o customer ser criado de forma assíncrona (evento SignedUp)  -> GET customers/{id} em loop
  5. PUT  customers/complete

Só mudam o que a arquitetura obriga: porta, prefixos de rota do gateway, e como o token trafega
(monólito: cookie "__access-token"; microsserviços: JSON accessToken + header Bearer).

Propriedades JMeter: -Jtest.duration, -Jtest.threads, -Jtest.rampup, -Japp.port, -Jtest.maxpolls
"""
from pathlib import Path
from xml.sax.saxutils import escape

SAIDA = Path(__file__).resolve().parent

ARQUITETURAS = {
    "InflowMonolito": dict(
        titulo="Inflow - Monolito modular",
        porta=8080,
        prefixo_users="",
        prefixo_customers="",
        json_id="$.id",
        bearer=False,
    ),
    "InflowMicrosservicos": dict(
        titulo="Inflow - Microsservicos",
        porta=5000,
        prefixo_users="/users-service",
        prefixo_customers="/customers-service",
        json_id="$.userId",
        bearer=True,
    ),
}


def prop(tipo, nome, valor):
    return f'<{tipo} name="{nome}">{escape(str(valor))}</{tipo}>'


def string(nome, valor):
    return prop("stringProp", nome, valor)


def boolean(nome, valor):
    return prop("boolProp", nome, str(valor).lower())


def header_manager(nome, headers):
    itens = "".join(
        f'<elementProp name="" elementType="Header">{string("Header.name", k)}{string("Header.value", v)}</elementProp>'
        for k, v in headers
    )
    return (
        f'<HeaderManager guiclass="HeaderPanel" testclass="HeaderManager" testname="{escape(nome)}" enabled="true">'
        f'<collectionProp name="HeaderManager.headers">{itens}</collectionProp></HeaderManager><hashTree/>'
    )


def groovy(tipo, nome, codigo):
    return (
        f'<{tipo} guiclass="TestBeanGUI" testclass="{tipo}" testname="{escape(nome)}" enabled="true">'
        f'{string("scriptLanguage", "groovy")}{string("parameters", "")}{string("filename", "")}'
        f'{string("cacheKey", "true")}{string("script", codigo)}</{tipo}><hashTree/>'
    )


def json_extractor(nome, variavel, caminho):
    return (
        f'<JSONPostProcessor guiclass="JSONPostProcessorGui" testclass="JSONPostProcessor" testname="{escape(nome)}" enabled="true">'
        f'{string("JSONPostProcessor.referenceNames", variavel)}'
        f'{string("JSONPostProcessor.jsonPathExprs", caminho)}'
        f'{string("JSONPostProcessor.match_numbers", "1")}'
        f'{string("JSONPostProcessor.defaultValues", "NOT_FOUND")}</JSONPostProcessor><hashTree/>'
    )


def sampler(nome, metodo, caminho, corpo=None, filhos=""):
    if corpo is None:
        argumentos = '<elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true"><collectionProp name="Arguments.arguments"/></elementProp>'
        raw = ""
    else:
        argumentos = (
            '<elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" testclass="Arguments" enabled="true">'
            '<collectionProp name="Arguments.arguments"><elementProp name="" elementType="HTTPArgument">'
            f'{boolean("HTTPArgument.always_encode", False)}{string("Argument.value", corpo)}{string("Argument.metadata", "=")}'
            "</elementProp></collectionProp></elementProp>"
        )
        raw = boolean("HTTPSampler.postBodyRaw", True)
    return (
        f'<HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="{escape(nome)}" enabled="true">'
        f"{argumentos}{raw}{string('HTTPSampler.path', caminho)}{string('HTTPSampler.method', metodo)}"
        f'{boolean("HTTPSampler.follow_redirects", True)}{boolean("HTTPSampler.use_keepalive", True)}'
        f'{boolean("HTTPSampler.auto_redirects", False)}</HTTPSamplerProxy><hashTree>{filhos}</hashTree>'
    )


def gerar(nome_arquivo, cfg):
    users = cfg["prefixo_users"]
    cust = cfg["prefixo_customers"]
    bearer = (
        header_manager("Authorization", [("Authorization", "Bearer ${accessToken}")])
        if cfg["bearer"]
        else ""
    )

    sign_up = sampler(
        "Sign-up",
        "POST",
        f"{users}/account/sign-up",
        '{"email":"${email}","password":"Secret123!"}',
        groovy(
            "JSR223PreProcessor",
            "Gerar dados unicos",
            'vars.put("email", UUID.randomUUID().toString() + "@inflow.io")\n'
            'vars.put("custName", UUID.randomUUID().toString().replace("-", "").substring(0, 12))',
        )
        + groovy(
            "JSR223PostProcessor",
            "Reiniciar espera do customer",
            'vars.put("custCode", "")\nvars.put("tries", "0")',
        ),
    )

    extratores = json_extractor("Extrair id do usuario", "userId", cfg["json_id"])
    if cfg["bearer"]:
        extratores += json_extractor("Extrair accessToken", "accessToken", "$.accessToken")
    sign_in = sampler(
        "Sign-in",
        "POST",
        f"{users}/account/sign-in",
        '{"email":"${email}","password":"Secret123!"}',
        extratores,
    )

    conta = sampler("Get account", "GET", f"{users}/account", filhos=bearer)

    espera_pp = groovy(
        "JSR223PostProcessor",
        "Registrar tentativa",
        'def codigo = prev.getResponseCode()\n'
        'vars.put("custCode", codigo)\n'
        'vars.put("tries", String.valueOf((vars.get("tries") as int) + 1))\n'
        '// 404 = customer ainda nao criado pelo evento assincrono: nao e falha, tenta de novo\n'
        'if (codigo == "404") { prev.setSuccessful(true) }',
    )
    espera_timer = (
        '<ConstantTimer guiclass="ConstantTimerGui" testclass="ConstantTimer" testname="Intervalo entre tentativas" enabled="true">'
        f'{string("ConstantTimer.delay", "50")}</ConstantTimer><hashTree/>'
    )
    espera_sampler = sampler(
        "Aguardar customer",
        "GET",
        f"{cust}/customers/${{userId}}",
        filhos=bearer + espera_pp + espera_timer,
    )
    condicao = (
        '${__groovy(vars.get("custCode") != "200" && (vars.get("tries") as int) < '
        'Integer.parseInt(props.getProperty("test.maxpolls", "200")))}'
    )
    while_ctrl = (
        '<WhileController guiclass="WhileControllerGui" testclass="WhileController" testname="Ate customer existir" enabled="true">'
        f'{string("WhileController.condition", condicao)}</WhileController><hashTree>{espera_sampler}</hashTree>'
    )

    complete = sampler(
        "Complete customer",
        "PUT",
        f"{cust}/customers/complete",
        '{"name":"${custName}","fullName":"John Doe","address":"Test street 123",'
        '"nationality":"PL","identityType":"id_card","identitySeries":"ABC1234567"}',
        bearer,
    )

    transacao = (
        '<TransactionController guiclass="TransactionControllerGui" testclass="TransactionController" testname="Fluxo completo" enabled="true">'
        f'{boolean("TransactionController.includeTimers", False)}{boolean("TransactionController.parent", True)}'
        f"</TransactionController><hashTree>{sign_up}{sign_in}{conta}{while_ctrl}{complete}</hashTree>"
    )

    defaults = (
        '<ConfigTestElement guiclass="HttpDefaultsGui" testclass="ConfigTestElement" testname="HTTP Request Defaults" enabled="true">'
        '<elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" testclass="Arguments" enabled="true"><collectionProp name="Arguments.arguments"/></elementProp>'
        f'{string("HTTPSampler.domain", "localhost")}{string("HTTPSampler.port", "${__P(app.port,%d)}" % cfg["porta"])}'
        f'{string("HTTPSampler.protocol", "http")}{string("HTTPSampler.contentEncoding", "UTF-8")}'
        f'{string("HTTPSampler.connect_timeout", "5000")}{string("HTTPSampler.response_timeout", "30000")}'
        "</ConfigTestElement><hashTree/>"
    )
    cookies = (
        '<CookieManager guiclass="CookiePanel" testclass="CookieManager" testname="Cookies" enabled="true">'
        f'<collectionProp name="CookieManager.cookies"/>{boolean("CookieManager.clearEachIteration", True)}'
        "</CookieManager><hashTree/>"
    )
    json_header = header_manager("Content-Type", [("Content-Type", "application/json")])

    grupo = (
        '<ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="Usuarios" enabled="true">'
        f'{string("ThreadGroup.on_sample_error", "continue")}'
        '<elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">'
        f'{boolean("LoopController.continue_forever", False)}{string("LoopController.loops", "-1")}</elementProp>'
        f'{string("ThreadGroup.num_threads", "${__P(test.threads,20)}")}'
        f'{string("ThreadGroup.ramp_time", "${__P(test.rampup,10)}")}'
        f'{boolean("ThreadGroup.scheduler", True)}'
        f'{string("ThreadGroup.duration", "${__P(test.duration,120)}")}{string("ThreadGroup.delay", "")}'
        f'{boolean("ThreadGroup.same_user_on_next_iteration", False)}</ThreadGroup>'
        f"<hashTree>{defaults}{cookies}{json_header}{transacao}</hashTree>"
    )

    plano = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3"><hashTree>'
        f'<TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="{escape(cfg["titulo"])}" enabled="true">'
        f'{boolean("TestPlan.functional_mode", False)}{boolean("TestPlan.serialize_threadgroups", False)}'
        '<elementProp name="TestPlan.user_defined_variables" elementType="Arguments" guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">'
        '<collectionProp name="Arguments.arguments"/></elementProp></TestPlan>'
        f"<hashTree>{grupo}</hashTree></hashTree></jmeterTestPlan>\n"
    )
    (SAIDA / f"{nome_arquivo}.jmx").write_text(plano, encoding="utf-8")
    print(f"Gerado: {SAIDA / (nome_arquivo + '.jmx')}")


if __name__ == "__main__":
    for nome, cfg in ARQUITETURAS.items():
        gerar(nome, cfg)
