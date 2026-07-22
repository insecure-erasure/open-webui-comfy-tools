        """
        Generate one image with optional control over model, seed, size, and steps.

        Display the generated image in your response using markdown:
            ![Generated image](image_url)

        prompt: Image generation prompt. Translate the user's request into English internally,
            then enrich with visual details without changing the subject or scene. Do not add
            superfluous details. Write the final prompt in English.
        model (optional): Only provide when the user explicitly requests a
            specific model.
        size (optional): Only provide when the user explicitly requests specific
            dimensions. Format as WxH (e.g., 2000x3000).
        steps (optional): Only provide when the user explicitly requests a
            specific number of steps.
        seed (optional): Only provide when the user explicitly
            requests a specific seed.
        """
