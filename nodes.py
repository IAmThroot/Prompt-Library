class PromptLibrary:
    """Two text boxes that you fill from the Prompt Library (or by hand).

    The text is stored in the workflow itself, so a workflow keeps working even if the
    prompt is later removed from the library. Connect the outputs to the `text` input
    of any CLIPTextEncode node (right-click the node > Convert widget to input).
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "positive": ("STRING", {"multiline": True, "default": "", "dynamicPrompts": True}),
                "negative": ("STRING", {"multiline": True, "default": "", "dynamicPrompts": True}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("positive", "negative")
    FUNCTION = "run"
    CATEGORY = "utils/prompt"

    def run(self, positive, negative):
        return (positive, negative)


NODE_CLASS_MAPPINGS = {"PromptLibrary": PromptLibrary}
NODE_DISPLAY_NAME_MAPPINGS = {"PromptLibrary": "Prompt Library"}
