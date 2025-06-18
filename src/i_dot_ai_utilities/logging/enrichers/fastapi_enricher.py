from i_dot_ai_utilities.logging.types.fastapi_enrichment_schema import ExtractedFastApiContext, RequestLike

class FastApiEnricher():
    @staticmethod
    def extract_context(logger, request: RequestLike) -> ExtractedFastApiContext | None:
        try: 
            if not isinstance(request, RequestLike):
                raise Exception(f"Exception(Logger): Request object of type {type(request)} doesn't conform to RequestLike. Context not set.")

            return {
                "request_method": request.method,
                "request_base_url": str(request.base_url),
                "request_user_agent": request.headers.get("user-agent", "none"),
                "request_path": request.url.path,
                "request_query": request.url.query                
            }
        except:
            logger.exception("Exception(Logger): Failed to extract FastAPI fields")
            return None
