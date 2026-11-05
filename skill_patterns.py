"""
The skill vocabulary: each skill name and the patterns that find it in a
posting.

It lives in its own module so both the report, which scores with it, and the
setup wizard, which tells users which names it recognises, can import it
without importing each other.
"""

SKILL_PATTERNS = {
    "Python": [r"\bpython\b"],
    "Java": [r"\bjava\b"],
    "JavaScript": [r"\bjavascript\b"],
    "TypeScript": [r"\btypescript\b"],
    "C++": [r"\bc\+\+\b"],
    "SQL": [r"\bsql\b", r"\bmysql\b", r"\bpostgres(?:ql)?\b", r"\bsqlite\b"],
    "AWS": [r"\baws\b", r"\bamazon web services\b"],
    "Azure": [r"\bazure\b"],
    "GCP": [r"\bgcp\b", r"\bgoogle cloud\b"],
    "React": [r"\breact\b"],
    "Node.js": [r"\bnode\.?js\b"],
    "Docker": [r"\bdocker\b"],
    "Git": [r"\bgit\b", r"\bgithub\b"],
    "Machine Learning": [r"\bmachine learning\b", r"\bml\b"],
    "Deep Learning": [r"\bdeep learning\b", r"\bneural network"],
    "TensorFlow": [r"\btensorflow\b"],
    "Keras": [r"\bkeras\b"],
    "TensorFlow/Keras": [r"\btensorflow\b", r"\bkeras\b"],
    "PyTorch": [r"\bpytorch\b"],
    "Scikit-learn": [r"\bscikit.?learn\b", r"\bsklearn\b"],
    "Pandas": [r"\bpandas\b"],
    "NumPy": [r"\bnumpy\b"],
    "Pandas/NumPy": [r"\bpandas\b", r"\bnumpy\b"],
    "NLP": [r"\bnlp\b", r"\bnatural language processing\b"],
    "LLMs": [r"\bllm", r"\blarge language model"],
    "NLP/LLMs": [r"\bnlp\b", r"\bllm", r"\blarge language model"],
    "RAG": [r"\brag\b", r"\bretrieval.?augmented\b"],
    "LangChain": [r"\blangchain\b"],
    "RAG/LangChain": [r"\brag\b", r"\bretrieval.?augmented\b", r"\blangchain\b"],
    "Computer Vision": [r"\bcomputer vision\b", r"\bimage processing\b"],
    "Time Series/Forecasting": [r"\btime.?series\b", r"\bforecast"],
    "Research/Publications": [r"\bresearch\b", r"\bpublication\b", r"\bieee\b",
                              r"\bscientific\b"],
    "Flask": [r"\bflask\b"],
    "FastAPI": [r"\bfastapi\b"],
    "Streamlit": [r"\bstreamlit\b"],
    "Flask/Streamlit": [r"\bflask\b", r"\bstreamlit\b"],
    "APIs/Cloud": [r"\bapi\b", r"\baws\b", r"\bazure\b", r"\bgcp\b", r"\bcloud\b"],
    "React/Node": [r"\breact\b", r"\bnode\.?js\b"],

    # Backend, data and infrastructure names. The original table covered
    # Python and ML well, so anyone outside that stack fell back to a literal
    # match on whatever they typed.
    "PostgreSQL": [r"\bpostgres(?:ql)?\b"],
    "MySQL": [r"\bmysql\b"],
    "MongoDB": [r"\bmongo(?:db)?\b"],
    "Redis": [r"\bredis\b"],
    "Elasticsearch": [r"\belastic ?search\b"],
    "Kubernetes": [r"\bkubernetes\b", r"\bk8s\b"],
    "Terraform": [r"\bterraform\b"],
    "Linux": [r"\blinux\b", r"\bunix\b"],
    "CI/CD": [r"\bci\s*/\s*cd\b", r"\bcontinuous (?:integration|delivery|deployment)\b",
              r"\bjenkins\b", r"\bgithub actions\b"],
    "Jenkins": [r"\bjenkins\b"],
    "Rust": [r"\brust\b"],
    "C#": [r"\bc#", r"\bc sharp\b", r"\bcsharp\b"],
    ".NET": [r"\.net\b", r"\bdotnet\b"],
    "Ruby": [r"\bruby\b"],
    "Ruby on Rails": [r"\bruby on rails\b", r"\brails\b"],
    "PHP": [r"\bphp\b"],
    "Kotlin": [r"\bkotlin\b"],
    "Swift": [r"\bswift\b", r"\bswiftui\b"],
    "Scala": [r"\bscala\b"],
    "Spark": [r"\bspark\b", r"\bpyspark\b"],
    "Kafka": [r"\bkafka\b"],
    "Airflow": [r"\bairflow\b"],
    "Hadoop": [r"\bhadoop\b"],
    "Snowflake": [r"\bsnowflake\b"],
    "Databricks": [r"\bdatabricks\b"],
    "dbt": [r"\bdbt\b"],
    "Tableau": [r"\btableau\b"],
    "Power BI": [r"\bpower ?bi\b"],
    "Angular": [r"\bangular\b"],
    "Vue": [r"\bvue(?:\.?js)?\b"],
    "Next.js": [r"\bnext\.?js\b"],
    "Django": [r"\bdjango\b"],
    "GraphQL": [r"\bgraphql\b"],
    "REST APIs": [r"\brest(?:ful)?\s*api", r"\brest\b(?=\s*api)"],
    "HTML/CSS": [r"\bhtml\b", r"\bcss\b"],
    "MATLAB": [r"\bmatlab\b"],
    "Bash/Shell": [r"\bbash\b", r"\bshell script", r"\bzsh\b"],
    "Agile/Scrum": [r"\bagile\b", r"\bscrum\b", r"\bkanban\b"],
    "Selenium": [r"\bselenium\b"],
    "pytest": [r"\bpytest\b"],

    # Names that are also ordinary English. A bare word-boundary match on
    # these fires on prose in almost every posting, which inflates every
    # score equally and destroys the ranking, so each is deliberately narrow.

    # "go fast", "go to market" must not count; a language list or an
    # explicit "with/in/using Go" must.
    "Go": [r"\bgolang\b", r"\bgo\b(?=\s*[,/])", r"\b(?:with|in|using)\s+go\b"],

    # "Spring 2027 internship" must not count.
    "Spring": [r"\bspring boot\b", r"\bspring framework\b", r"\bspring mvc\b"],

    # "candidates who excel at" must not count.
    "Excel": [r"\bmicrosoft excel\b", r"\bms excel\b",
              r"\bexcel\b(?!\s+(?:at|in|as))"],
}
