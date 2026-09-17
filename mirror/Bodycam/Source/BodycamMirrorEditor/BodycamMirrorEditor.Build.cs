using UnrealBuildTool;

public class BodycamMirrorEditor : ModuleRules {
    public BodycamMirrorEditor(ReadOnlyTargetRules Target) : base(Target) {
        PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
        PublicDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine" });
        PrivateDependencyModuleNames.AddRange(new string[] {
            "UnrealEd", "BlueprintGraph", "Kismet", "KismetCompiler", "GraphEditor", "EditorSubsystem", "SubobjectDataInterface",
            "Slate", "SlateCore", "Bodycam", "Json", "JsonUtilities", "UMG", "GameplayAbilities", "GameplayTags"
        });
    }
}
