#pragma once
#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "BodycamMirrorTools.generated.h"

class UBlueprint;
class UUserDefinedEnum;

/** Editor-only helpers driven from Scripts/make_blueprints.py (Python) — not part of the shipped gamemode. */
UCLASS()
class BODYCAMMIRROREDITOR_API UBodycamMirrorTools : public UBlueprintFunctionLibrary {
    GENERATED_BODY()
public:
    /** Paste a graph exported as text (the same format the editor uses for Ctrl+C on nodes) into the named graph of a Blueprint.
     *  GraphName "EventGraph" = the ubergraph; otherwise a function graph of that name (created if missing).
     *  Every imported node is reconstructed afterwards so pins missing from the text are regenerated from the function/variable. */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static FString ImportGraphText(UBlueprint* Blueprint, const FString& GraphName, const FString& Text, bool bReconstructNodes = true);

    /** Compile and return every warning/error as text ("" when clean). */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static FString CompileAndReport(UBlueprint* Blueprint);

    /** One line per reflected property of the class chain, in the exact order the cooked (unversioned) format uses. */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static FString DescribeClassLayout(UClass* Class, bool bIncludeSuper = true);

    /** Add enumerators to a UserDefinedEnum (created by the Python side). Display names = the given names. */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static bool SetUserEnumerators(UUserDefinedEnum* Enum, const TArray<FString>& Names);

    /** Put a 'BeginPlay -> Print String(Text)' pair into the event graph (gives the class an UberGraphFrame like the game's Blueprints have). */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static FString AddBeginPlayPrint(UBlueprint* Blueprint, const FString& Text);

    /** Add a member variable. Category: bool, byte, int, int64, float (double), name, string, text, object, class, struct.
     *  SubObject = the class (object/class) or script struct (struct) for those categories. */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static bool AddVariable(UBlueprint* Blueprint, const FString& Name, const FString& Category, UObject* SubObject, bool bArray = false);

    /** Add a component (SCS node) under the root and name its variable. Returns the variable name actually used. */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static FString AddComponent(UBlueprint* Blueprint, UClass* ComponentClass, const FString& VariableName);

    /** Build nodes + wires in a graph from a JSON description (see Scripts/ctf_graphs.py for the schema). Returns a report. */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static FString BuildGraph(UBlueprint* Blueprint, const FString& GraphName, const FString& JsonText);

    /** Create a function graph that overrides a parent function (entry/result nodes carry the parent's signature). */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static FString OverrideFunction(UBlueprint* Blueprint, const FString& FunctionName);

    /** Create an empty user function graph (entry node only) if it does not exist. */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static FString EnsureFunctionGraph(UBlueprint* Blueprint, const FString& FunctionName);

    /** Mark a member variable replicated (optionally with a RepNotify function of the given name). */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static bool SetVariableReplicated(UBlueprint* Blueprint, const FString& VariableName, bool bReplicated, const FString& RepNotifyFunction = TEXT(""));

    UFUNCTION(BlueprintCallable, Category="BodycamMirror")
    static bool SetVariableSaveGame(UBlueprint* Blueprint, const FString& VariableName);

    /** Class defaults of an SCS widget component: the widget class it creates at BeginPlay and screen/world space. */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static FString SetWidgetComponentDefaults(UBlueprint* Blueprint, const FString& VariableName, UClass* WidgetClass, bool bScreenSpace = true);

    /** Tags a GameplayEffect Blueprint grants to its target while active. */
    UFUNCTION(BlueprintCallable, Category="Bodycam Mirror")
    static FString SetGameplayEffectGrantedTags(UBlueprint* Blueprint, const TArray<FString>& Tags);

    /** Append one attribute modifier to a GameplayEffect blueprint's CDO: Attribute = <AttributeSetClass>.<AttributeName>,
     *  Op = "Add" | "Multiply" | "Divide" | "Override", constant magnitude (scalable float). */
    UFUNCTION(BlueprintCallable, Category = "Bodycam Mirror")
    static FString AddGameplayEffectModifier(UBlueprint* Blueprint, UClass* AttributeSetClass, const FString& AttributeName, const FString& Op, float Magnitude);

    /** Find a function graph or the event graph by name. */
    static UEdGraph* FindGraph(UBlueprint* Blueprint, const FString& GraphName, bool bCreateFunctionGraph);
};
