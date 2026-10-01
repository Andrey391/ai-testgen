// Regression run of AI Test Generator tests in TeamCity (Kotlin DSL: .teamcity/settings.kts of the application).
//
// Expects the studio's data folder under qa/testgen-data (TESTGEN_DATA_DIR) and parameters
// (Parameters → add as passwords): env.TESTGEN_USERNAME, env.TESTGEN_PASSWORD and, optionally,
// env.ANTHROPIC_API_KEY (or the key of your model provider) for self-healing and failure analysis.
// The build fails when a test outside quarantine fails (exit code 1); flaky tests do not fail it.
import jetbrains.buildServer.configs.kotlin.*
import jetbrains.buildServer.configs.kotlin.buildFeatures.xmlReport
import jetbrains.buildServer.configs.kotlin.buildFeatures.XmlReport
import jetbrains.buildServer.configs.kotlin.buildSteps.script
import jetbrains.buildServer.configs.kotlin.triggers.schedule

version = "2025.07"

project {
    buildType(UiRegression)
}

object UiRegression : BuildType({
    name = "UI regression (AI Test Generator)"
    artifactRules = "reports => reports\nqa/testgen-data/projects/*/runs => runs"

    params {
        param("env.TESTGEN_DATA_DIR", "%teamcity.build.checkoutDir%/qa/testgen-data")
        param("testgen.project", "My project")
        param("testgen.tag", "smoke")
        param("testgen.browser", "chromium")
        password("env.TESTGEN_USERNAME", "")
        password("env.TESTGEN_PASSWORD", "")
        password("env.ANTHROPIC_API_KEY", "")
    }

    steps {
        script {
            name = "Install the studio"
            scriptContent = """
                git clone --depth 1 https://git.example.com/qa/ai-testgen.git .testgen
                python -m pip install -r .testgen/requirements.txt
                python -m playwright install --with-deps %testgen.browser%
            """.trimIndent()
            dockerImage = "mcr.microsoft.com/playwright/python:v1.55.0-noble"
        }
        script {
            name = "Run tests"
            workingDir = ".testgen"
            scriptContent = """
                python -m testgen.run --project "%testgen.project%" --tag "%testgen.tag%" --browser %testgen.browser% \
                  --junit "%teamcity.build.checkoutDir%/reports/junit.xml" --allure "%teamcity.build.checkoutDir%/reports/allure"
            """.trimIndent()
            dockerImage = "mcr.microsoft.com/playwright/python:v1.55.0-noble"
        }
    }

    features {
        xmlReport {
            reportType = XmlReport.XmlReportType.JUNIT
            rules = "reports/junit.xml"
        }
    }

    triggers {
        schedule {
            schedulingPolicy = daily { hour = 3 }
            triggerBuild = always()
        }
    }
})
