package main

import (
	"fmt"
	"time"
)

func main() {
	fmt.Println("=== TEST START ===")
	for i := 0; i < 5; i++ {
		fmt.Printf("Count: %d\n", i)
		time.Sleep(2 * time.Second)
	}
	fmt.Println("=== TEST END ===")
}