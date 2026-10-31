#$ per million tokens: (input, output, cache read). Cache writes cost 1.25x the input price.
#Haiku's prices are for prompts up to 100K tokens, which covers the router.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
}

#a model missing from the table (for example a fallback model) is priced like the most
#expensive one, so the spend cap stops too early rather than too late
UNKNOWN_MODEL_PRICE = (10.00, 50.00, 0.25)


#dollar cost of one API call, from the model that answered and the usage it reported
def call_cost(model, usage):
    input_price, output_price, cache_read_price = PRICES.get(model, UNKNOWN_MODEL_PRICE)
    return (
        usage.input_tokens * input_price
        + (usage.cache_creation_input_tokens or 0) * input_price * 1.25
        + (usage.cache_read_input_tokens or 0) * cache_read_price
        + usage.output_tokens * output_price
    ) / 1_000_000


#dollar cost of a whole response. After a fallback, usage.iterations has one entry per model that ran,
#each billed at its own prices; the entry for the model that was asked for has no model name.
def response_cost(response, requested_model):
    iterations = [entry for entry in (getattr(response.usage, "iterations", None) or [])
                  if entry.type in ("message", "fallback_message")]
    if not iterations:
        return call_cost(response.model, response.usage)
    return sum(call_cost(entry.model or requested_model, entry) for entry in iterations)
