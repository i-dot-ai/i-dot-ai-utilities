# Structured Logging

## Usage

### Create a Logger

You can create the most basic version of the logger by simply instantiating a new object of the logger class.
```python
logger = StructuredLogger()

...
logger.info(...)
```
This is enough to format your logs for consumption by downstream log subscribers.

<br>

***

<br>

A more productionised version might look something like this - this uses console-based logging when running locally, and structures logging into JSON/enriches context when running in ECS (Fargate).
```python
environment = os.environ.get('ENVIRONMENT')
logger_environment = ExecutionEnvironmentType.LOCAL if environment == "LOCAL" else ExecutionEnvironmentType.FARGATE
logger_format = LogOutputFormat.TEXT if environment == "LOCAL" else LogOutputFormat.JSON

logger = StructuredLogger(level='info', options={
    "execution_environment": logger_environment,
    "log_format": logger_format,
})
```

<br>

***

<br>

### Creating Log Messages
Once the logger is initialised, you can create log messages in different ways depending on your requirement.
For example, you can create simple messages with string-literals:

```python
logger.info("A thing happened")
```

Or you might add some context fields to the log message so they become available in the downstream logging stack:
```python
logger.info("Anothing thing happened", thing_id=12345, user_logged_in=True)
```

You can also format strings so the message field itself contains useful data, whilst also capturing that useful data as separate fields:
```python
logger.info("Yet another thing occurred for user {email} with id {id}", email=user_email, id=id)
```

It is best practice to NOT to use f-strings in log message creation. Variable interpolation happens within the function itself and allows the library to extract message content correctly so it can be indexed downstream.

<br>

***

<br>

Exceptions are added to the message output automatically when called inside of an `except` block:
```python
logger.exception("Something went wrong when user {email} logging in", email=email)
```

This will log the message, and inject the exception into the log context automatically.

<br>

***

<br>

### Refreshing the Logger

You should always refresh the logger context at the entrypoints of your API/application. This generates a new context ID and resets the logger for that invocation by removing any custom fields that were previously added by enrichers (see examples below) or through `set_context_field()`.
```python
@app.post("/do/the/thing")
async def do_the_thing(request: Request):

    logger.refresh_context()

    do_stuff()
    ...
```

<br>

***

<br>

### Setting Execution Environment Type and Log Format

#### Environment Type

Environment Type is configurable using the `execution_environment` option setting when instantiating the logger. Accepted values are defined in the `ExecutionEnvironmentType` enum (see [here](./types/enrichment_types.py)).

The logger will automatically extract important information from the execution environment and enrich log messages using it. 

#### Log Format

Log format is configurable using the `log_format` option setting when instantiating the logger. Accepted values are defined in the `LogOutputFormat` enum (see [here](./types/log_output_format.py)).

The ability to change log formats exists to give developers a friendlier log output when developing locally. They should be set to JSON format when running on the platform so structured logs can be read and processed in our logging stack downstream.

<br>

***

<br>

### Implementing Persistent Context
Context can be automatically be added to log messages by using enrichers. Enrichers are helpers provided to you to automatically extract context related to a given execution:
```python
@app.get("/")
async def root(request: Request):

    logger.refresh_context(context_enrichers=[
        {
            "type": ContextEnrichmentType.FASTAPI,
            "object": request,
        }
    ])

    do_stuff()
```
The above example would extract information from the FastAPI request object (query string, path, user agent, etc) and inject it into all subsequent log messages until `refresh_context()` is called again.

Each Context Enricher object accepts a `type` and `object`. `type` is a `ContextEnrichmentType` enum ([see here](./types/enrichment_types.py) for accepted values). `object` is the Input Object to pass into the logger for context extraction - see the table below for further details.

The following context enrichers are available for use:
| Name | Input Object | Extracted Fields |
|---------|-------------------------------------------------------------------------------------------------------|----------------------------------------------------------------|
| FastAPI / Starlette | [Request Object](httpsd:'\[]:?PO-//fastapi.tiangolo.com/advanced/using-request-directly/#details-about-the-request-object) | See [FastApiRequestMetadata](./types/fastapi_enrichment_schema.py#31) |
| Lambda  | [Lambda Context Object](https://docs.aws.amazon.com/lambda/latest/dg/python-context.html) | See [LambdaContextMetadata](./types/lambda_enrichment_schema.py) |


<br>

***

<br>

### Adding your own context fields

You can add custom fields to your logger, which will appear on each log message going forward until `refresh_context()` is called again. This is useful for enriching your own context onto log messages once important information has been discovered during execution.
```python
@app.post("/login")
async def login(request: Request):
    logger.refresh_context()

    user_email = get_user_info(request)

    logger.info("User {email} started login process", email=user_email)
    logger.set_context_field("email", user_email)

    ...
```
Fields added in this manner will be indexed and searchable in the downstream logging stack.
