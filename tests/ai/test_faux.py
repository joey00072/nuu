import pytest
from nuu.ai.providers.faux import register_faux_provider, faux_assistant_message
from nuu.ai.stream import complete
from nuu.ai.types import Context


@pytest.mark.asyncio
async def test_faux_provider():
    faux = register_faux_provider()
    model = faux.get_model()

    faux.set_responses(
        [
            faux_assistant_message("Hello, I am faux!"),
            faux_assistant_message("Second response"),
        ]
    )

    context = Context(messages=[])

    resp1 = await complete(model, context)
    assert resp1.content[0].text == "Hello, I am faux!"

    resp2 = await complete(model, context)
    assert resp2.content[0].text == "Second response"
