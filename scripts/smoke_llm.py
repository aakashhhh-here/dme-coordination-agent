"""One real Claude call, to prove credentials work."""

from dme.app import make_llm

question = "In one sentence: what is HCPCS code K0001?"
print(
    make_llm().complete(
        purpose="smoke", system="Be brief.", messages=[{"role": "user", "content": question}]
    )
)
